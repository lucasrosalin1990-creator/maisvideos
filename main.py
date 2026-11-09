import os
import re
import uuid
import random
import shutil
import zipfile
import subprocess
from typing import List, Dict, Any
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="+VIDEOS Cloud Engine", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent
TEMP_DIR = BASE_DIR / "temp_processing"
TEMP_DIR.mkdir(exist_ok=True)

# GERENCIADOR DE TAREFAS NA NUVEM
TAREFAS: Dict[str, Dict[str, Any]] = {}


def obter_duracao(caminho: Path) -> float:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(caminho)
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        return float(res.stdout.strip())
    except Exception:
        return 10.0


def detectar_limiar_inteligente(caminho: Path, sensibilidade: str) -> tuple:
    """Mede o volume médio real do vídeo para calibrar o corte perfeitamente em iPhones e celulares."""
    cmd_vol = ["ffmpeg", "-y", "-i", str(caminho), "-af", "volumedetect", "-f", "null", "-"]
    res = subprocess.run(cmd_vol, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    match = re.search(r"mean_volume:\s*(-?\d+\.?\d*)\s*dB", res.stderr)
    mean_vol = float(match.group(1)) if match else -18.0

    if sensibilidade == "suave":
        limiar_db = max(-34.0, min(-18.0, mean_vol - 8.0))
        dur_min = 0.32
    elif sensibilidade == "agressivo":
        limiar_db = max(-24.0, min(-11.0, mean_vol - 2.5))
        dur_min = 0.16
    else:  # equilibrado
        limiar_db = max(-28.0, min(-14.0, mean_vol - 5.0))
        dur_min = 0.22

    return f"{limiar_db:.1f}dB", dur_min


def cortar_pausas_ffmpeg(video_in: Path, video_out: Path, sensibilidade: str, pad_9_16: bool = False) -> tuple:
    """Remove os silêncios de um vídeo e retorna (duracao_antes, duracao_depois)."""
    dur_antes = obter_duracao(video_in)
    db_limiar, dur_min = detectar_limiar_inteligente(video_in, sensibilidade)

    cmd_detect = [
        "ffmpeg", "-y", "-i", str(video_in),
        "-af", f"silencedetect=noise={db_limiar}:d={dur_min}",
        "-f", "null", "-"
    ]
    res_detect = subprocess.run(cmd_detect, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
    stderr = res_detect.stderr

    starts = [float(x) for x in re.findall(r"silence_start:\s*(\d+\.?\d*)", stderr)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*(\d+\.?\d*)", stderr)]

    # Se o silêncio foi até o final do vídeo sem fechar silence_end
    if len(starts) > len(ends):
        ends.append(dur_antes)

    segmentos = []
    ultimo = 0.0
    for s, e in zip(starts, ends):
        if s > ultimo + 0.08:
            ini = max(0.0, ultimo - 0.04)
            fim = min(dur_antes, s + 0.04)
            segmentos.append((ini, fim))
        ultimo = e

    if ultimo < dur_antes - 0.08:
        ini = max(0.0, ultimo - 0.04)
        segmentos.append((ini, dur_antes))

    escala_9_16 = ",scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1" if pad_9_16 else ""

    if not segmentos or (len(segmentos) == 1 and (segmentos[0][1] - segmentos[0][0]) >= dur_antes - 0.15):
        vf_filter = escala_9_16.lstrip(",") if pad_9_16 else "null"
        cmd_cut = [
            "ffmpeg", "-y", "-i", str(video_in),
            "-vf", vf_filter,
            "-r", "30", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", str(video_out)
        ]
    else:
        select_v = "+".join([f"between(t,{s:.3f},{e:.3f})" for s, e in segmentos])
        vf_full = f"select='{select_v}',setpts=N/FRAME_RATE/TB{escala_9_16}"
        af_full = f"aselect='{select_v}',asetpts=N/SR/TB"
        cmd_cut = [
            "ffmpeg", "-y", "-i", str(video_in),
            "-vf", vf_full,
            "-af", af_full,
            "-r", "30", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", str(video_out)
        ]

    subprocess.run(cmd_cut, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    dur_depois = obter_duracao(video_out)
    return dur_antes, dur_depois


def padronizar_apenas_9_16(video_in: Path, video_out: Path):
    """Padroniza para 9:16 (1080x1920) sem cortar nenhuma pausa."""
    cmd = [
        "ffmpeg", "-y", "-i", str(video_in),
        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1",
        "-r", "30", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", str(video_out)
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ============================================================================
# TAREFA 1: REMOVEDOR DE PAUSAS NO VÍDEO
# ============================================================================
def task_remover_pausas(task_id: str, caminhos_videos: List[Path], prefixo: str, sensibilidade: str):
    task_dir = TEMP_DIR / task_id
    out_dir = task_dir / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)

    TAREFAS[task_id]["status"] = "processando"
    TAREFAS[task_id]["progresso"] = 10
    TAREFAS[task_id]["mensagem"] = "Calibrando decibéis e removendo pausas..."

    arquivos_prontos = []
    lista_metadados = []
    total = len(caminhos_videos)

    try:
        for idx, video_in in enumerate(caminhos_videos):
            if TAREFAS[task_id].get("cancelado"):
                return

            nome_saida = f"{prefixo}_{idx+1:02d}.mp4"
            video_out = out_dir / nome_saida

            dur_antes, dur_depois = cortar_pausas_ffmpeg(video_in, video_out, sensibilidade, pad_9_16=False)
            arquivos_prontos.append(video_out)

            economia = max(0, int(round(((dur_antes - dur_depois) / max(0.1, dur_antes)) * 100)))
            lista_metadados.append({
                "nome": nome_saida,
                "url": f"/api/arquivo/{task_id}/{nome_saida}",
                "info": f"{dur_antes:.1f}s ➔ {dur_depois:.1f}s (-{economia}%)"
            })

            prog = 10 + int(((idx + 1) / total) * 80)
            TAREFAS[task_id]["progresso"] = prog
            TAREFAS[task_id]["mensagem"] = f"Cortando pausas do vídeo {idx+1} de {total}..."

        zip_path = task_dir / f"{prefixo}_Videos_Sem_Pausa.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for v in arquivos_prontos:
                z.write(v, arcname=v.name)

        shutil.rmtree(task_dir / "inputs", ignore_errors=True)

        TAREFAS[task_id]["status"] = "concluido"
        TAREFAS[task_id]["progresso"] = 100
        TAREFAS[task_id]["mensagem"] = f"Concluído! {total} vídeo(s) processado(s) sem pausas."
        TAREFAS[task_id]["download_url"] = f"/api/download/{task_id}"
        TAREFAS[task_id]["arquivos"] = lista_metadados

    except Exception as e:
        TAREFAS[task_id]["status"] = "erro"
        TAREFAS[task_id]["mensagem"] = f"Erro no servidor: {str(e)}"


# ============================================================================
# TAREFA 2 E 3: MULTIPLICADOR (PURO OU COM REMOÇÃO DE PAUSAS)
# ============================================================================
def task_multiplicar_videos(
    task_id: str,
    ganchos: List[Path],
    corpos: List[Path],
    ctas: List[Path],
    prefixo: str,
    remover_pausas: bool,
    sensibilidade: str
):
    task_dir = TEMP_DIR / task_id
    out_dir = task_dir / "outputs"
    norm_dir = task_dir / "norm"
    out_dir.mkdir(parents=True, exist_ok=True)
    norm_dir.mkdir(parents=True, exist_ok=True)

    TAREFAS[task_id]["status"] = "processando"
    TAREFAS[task_id]["progresso"] = 5
    TAREFAS[task_id]["mensagem"] = (
        "Removendo pausas de todos os blocos e padronizando 9:16..."
        if remover_pausas else
        "Padronizando Ganchos, Corpos e CTAs em formato 9:16..."
    )

    try:
        total_blocos = len(ganchos) + len(corpos) + len(ctas)
        blocos_processados = 0

        def preparar_bloco(caminho_in: Path, tag: str) -> Path:
            nonlocal blocos_processados
            saida = norm_dir / f"prep_{tag}.mp4"
            if remover_pausas:
                cortar_pausas_ffmpeg(caminho_in, saida, sensibilidade, pad_9_16=True)
            else:
                padronizar_apenas_9_16(caminho_in, saida)

            blocos_processados += 1
            TAREFAS[task_id]["progresso"] = 5 + int((blocos_processados / max(1, total_blocos)) * 35)
            return saida

        norm_g = [preparar_bloco(g, f"g{i}") for i, g in enumerate(ganchos)]
        norm_c = [preparar_bloco(c, f"c{i}") for i, c in enumerate(corpos)]
        norm_t = [preparar_bloco(t, f"t{i}") for i, t in enumerate(ctas)]

        if TAREFAS[task_id].get("cancelado"):
            return

        TAREFAS[task_id]["progresso"] = 42
        TAREFAS[task_id]["mensagem"] = "Embaralhando combinações de vídeos..."

        combinacoes = []
        for g in norm_g:
            for c in norm_c:
                for t in norm_t:
                    combinacoes.append((g, c, t))

        random.shuffle(combinacoes)
        total_vids = len(combinacoes)
        finais = []
        lista_metadados = []

        for idx, (g, c, t) in enumerate(combinacoes):
            if TAREFAS[task_id].get("cancelado"):
                return

            nome_arq = f"{prefixo}_{idx+1:02d}.mp4"
            criativo_path = out_dir / nome_arq

            concat_txt = task_dir / f"list_{idx}.txt"
            with open(concat_txt, "w", encoding="utf-8") as f:
                f.write(f"file '{g.resolve()}'\nfile '{c.resolve()}'\nfile '{t.resolve()}'\n")

            cmd_concat = [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", str(concat_txt),
                "-c", "copy", str(criativo_path)
            ]
            subprocess.run(cmd_concat, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            concat_txt.unlink(missing_ok=True)
            finais.append(criativo_path)

            dur_final = obter_duracao(criativo_path)
            lista_metadados.append({
                "nome": nome_arq,
                "url": f"/api/arquivo/{task_id}/{nome_arq}",
                "info": f"Duração: {dur_final:.1f}s • 9:16"
            })

            prog = 42 + int(((idx + 1) / total_vids) * 50)
            TAREFAS[task_id]["progresso"] = prog
            TAREFAS[task_id]["mensagem"] = f"Montando vídeo {idx+1} de {total_vids}..."

        zip_path = task_dir / f"{prefixo}_Lote_Multiplicado.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for f in finais:
                z.write(f, arcname=f.name)

        shutil.rmtree(norm_dir, ignore_errors=True)
        shutil.rmtree(task_dir / "inputs", ignore_errors=True)

        TAREFAS[task_id]["status"] = "concluido"
        TAREFAS[task_id]["progresso"] = 100
        TAREFAS[task_id]["mensagem"] = f"{total_vids} vídeos gerados e embaralhados com sucesso!"
        TAREFAS[task_id]["download_url"] = f"/api/download/{task_id}"
        TAREFAS[task_id]["arquivos"] = lista_metadados

    except Exception as e:
        TAREFAS[task_id]["status"] = "erro"
        TAREFAS[task_id]["mensagem"] = f"Erro na multiplicação: {str(e)}"


# ============================================================================
# ROTAS DA API
# ============================================================================
@app.get("/")
def index():
    idx_path = BASE_DIR / "index.html"
    if idx_path.exists():
        return FileResponse(idx_path)
    return {"status": "+VIDEOS Cloud Online"}


@app.post("/api/remover-pausas")
async def rota_remover_pausas(
    background_tasks: BackgroundTasks,
    arquivos: List[UploadFile] = File(...),
    prefixo: str = Form("Meu_Video"),
    sensibilidade: str = Form("equilibrado")
):
    task_id = str(uuid.uuid4())
    in_dir = TEMP_DIR / task_id / "inputs"
    in_dir.mkdir(parents=True, exist_ok=True)

    salvos = []
    for idx, f in enumerate(arquivos):
        ext = Path(f.filename).suffix or ".mp4"
        dest = in_dir / f"raw_{idx}{ext}"
        with open(dest, "wb") as b:
            shutil.copyfileobj(f.file, b)
        salvos.append(dest)

    TAREFAS[task_id] = {
        "id": task_id,
        "status": "pendente",
        "progresso": 5,
        "mensagem": "Iniciando corte no servidor...",
        "download_url": None,
        "arquivos": [],
        "cancelado": False
    }

    background_tasks.add_task(task_remover_pausas, task_id, salvos, prefixo, sensibilidade)
    return {"task_id": task_id}


@app.post("/api/multiplicar")
async def rota_multiplicar(
    background_tasks: BackgroundTasks,
    ganchos: List[UploadFile] = File(...),
    corpos: List[UploadFile] = File(...),
    ctas: List[UploadFile] = File(...),
    prefixo: str = Form("Meu_Video"),
    remover_pausas: str = Form("false"),
    sensibilidade: str = Form("equilibrado")
):
    task_id = str(uuid.uuid4())
    in_dir = TEMP_DIR / task_id / "inputs"
    in_dir.mkdir(parents=True, exist_ok=True)

    def salvar_lote(arqs, sub):
        dest_dir = in_dir / sub
        dest_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for idx, f in enumerate(arqs):
            ext = Path(f.filename).suffix or ".mp4"
            p = dest_dir / f"{sub}_{idx}{ext}"
            with open(p, "wb") as b:
                shutil.copyfileobj(f.file, b)
            paths.append(p)
        return paths

    g_paths = salvar_lote(ganchos, "g")
    c_paths = salvar_lote(corpos, "c")
    t_paths = salvar_lote(ctas, "t")

     cortar = remover_pausas.lower() == "true"

    TAREFAS[task_id] = {
        "id": task_id,
        "status": "pendente",
        "progresso": 5,
        "mensagem": "Iniciando processamento em nuvem...",
        "download_url": None,
        "arquivos": [],
        "cancelado": False
    }

    background_tasks.add_task(task_multiplicar_videos, task_id, g_paths, c_paths, t_paths, prefixo, cortar, sensibilidade)
    return {"task_id": task_id}


@app.post("/api/cancelar/{task_id}")
async def cancelar_tarefa(task_id: str):
    if task_id in TAREFAS:
        TAREFAS[task_id]["cancelado"] = True
        TAREFAS[task_id]["status"] = "cancelado"
        TAREFAS[task_id]["mensagem"] = "Processo interrompido pelo usuário."
    return {"ok": True}


@app.get("/api/tarefa/{task_id}")
async def checar_status_tarefa(task_id: str):
    if task_id not in TAREFAS:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
    return TAREFAS[task_id]


@app.get("/api/arquivo/{task_id}/{filename}")
async def servir_video_individual(task_id: str, filename: str):
    arq_path = TEMP_DIR / task_id / "outputs" / filename
    if not arq_path.exists():
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")
    return FileResponse(arq_path, filename=filename, media_type="video/mp4")


@app.get("/api/download/{task_id}")
async def download_zip(task_id: str):
    task_dir = TEMP_DIR / task_id
    zips = list(task_dir.glob("*.zip"))
    if not zips:
        raise HTTPException(status_code=404, detail="Arquivo ZIP não encontrado")
    return FileResponse(zips[0], filename=zips[0].name, media_type="application/zip")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
