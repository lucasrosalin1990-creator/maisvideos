import os
import re
import uuid
import json
import random
import shutil
import zipfile
import subprocess
from typing import List, Dict, Any
from pathlib import Path

import requests
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="+VIDEOS Cloud Micro SaaS", version="2.0.0")

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

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# FILA DE TAREFAS NA NUVEM
TAREFAS: Dict[str, Dict[str, Any]] = {}

def fallback_roteiro(produto: str) -> dict:
    return {
        "gancho": f"Pare de jogar dinheiro fora com ar-condicionado! Esse {produto} mudou meu quarto...",
        "corpo": f"Mostre o produto funcionando nos primeiros 10 segundos provando o resultado prático sem esforço nenhum.",
        "cta": f"Clique no carrinho do TikTok Shop abaixo antes que o lote com frete grátis acabe hoje."
    }

# ============================================================================
# PROCESSAMENTO FFmpeg 1: REMOVEDOR REAL DE PAUSAS
# ============================================================================
def task_remover_pausas(task_id: str, caminhos_videos: List[Path], prefixo: str, sensibilidade: str):
    task_dir = TEMP_DIR / task_id
    out_dir = task_dir / "limpos"
    out_dir.mkdir(parents=True, exist_ok=True)

    TAREFAS[task_id]["status"] = "processando"
    TAREFAS[task_id]["progresso"] = 15
    TAREFAS[task_id]["mensagem"] = "Analisando frequências de áudio com FFmpeg..."

    db_limiar = "-30dB"
    if sensibilidade == "suave":
        db_limiar = "-36dB"
    elif sensibilidade == "agressivo":
        db_limiar = "-24dB"

    videos_processados = []
    total = len(caminhos_videos)

    try:
        for idx, video_in in enumerate(caminhos_videos):
            video_out = out_dir / f"{prefixo}_{idx+1:02d}.mp4"

            # 1. Detecta silêncios com FFmpeg
            cmd_detect = [
                "ffmpeg", "-y", "-i", str(video_in),
                "-af", f"silencedetect=noise={db_limiar}:d=0.3",
                "-f", "null", "-"
            ]
            res_detect = subprocess.run(cmd_detect, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
            stderr = res_detect.stderr

            starts = [float(x) for x in re.findall(r"silence_start: (\d+\.?\d*)", stderr)]
            ends = [float(x) for x in re.findall(r"silence_end: (\d+\.?\d*)", stderr)]

            cmd_dur = [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(video_in)
            ]
            res_dur = subprocess.run(cmd_dur, stdout=subprocess.PIPE, text=True)
            duracao = float(res_dur.stdout.strip() or 18.0)

            segmentos = []
            ultimo = 0.0
            for s, e in zip(starts, ends):
                if s > ultimo:
                    segmentos.append((ultimo, s))
                ultimo = e
            if ultimo < duracao:
                segmentos.append((ultimo, duracao))

            if not segmentos or (len(segmentos) == 1 and segmentos[0][0] == 0.0):
                cmd_cut = [
                    "ffmpeg", "-y", "-i", str(video_in),
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
                    "-c:a", "aac", "-b:a", "128k", str(video_out)
                ]
            else:
                select_v = "+".join([f"between(t,{s:.2f},{e:.2f})" for s, e in segmentos])
                cmd_cut = [
                    "ffmpeg", "-y", "-i", str(video_in),
                    "-vf", f"select='{select_v}',setpts=N/FRAME_RATE/TB",
                    "-af", f"aselect='{select_v}',asetpts=N/SR/TB",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22",
                    "-c:a", "aac", "-b:a", "128k", str(video_out)
                ]

            subprocess.run(cmd_cut, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            videos_processados.append(video_out)

            prog = 15 + int(((idx + 1) / total) * 75)
            TAREFAS[task_id]["progresso"] = prog
            TAREFAS[task_id]["mensagem"] = f"Cortando pausas do vídeo {idx+1} de {total}..."

        zip_path = task_dir / f"{prefixo}_Videos_Limpos.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for v in videos_processados:
                z.write(v, arcname=v.name)

        shutil.rmtree(task_dir / "inputs", ignore_errors=True)

        TAREFAS[task_id]["status"] = "concluido"
        TAREFAS[task_id]["progresso"] = 100
        TAREFAS[task_id]["mensagem"] = f"Sucesso! {total} vídeos limpos na nuvem."
        TAREFAS[task_id]["download_url"] = f"/api/download/{task_id}"

    except Exception as e:
        TAREFAS[task_id]["status"] = "erro"
        TAREFAS[task_id]["mensagem"] = f"Falha no processamento: {str(e)}"

# ============================================================================
# PROCESSAMENTO FFmpeg 2: MULTIPLICADOR 9:16 COM SHUFFLE
# ============================================================================
def task_multiplicar_videos(task_id: str, ganchos: List[Path], corpos: List[Path], ctas: List[Path], prefixo: str):
    task_dir = TEMP_DIR / task_id
    out_dir = task_dir / "criativos"
    norm_dir = task_dir / "norm"
    out_dir.mkdir(parents=True, exist_ok=True)
    norm_dir.mkdir(parents=True, exist_ok=True)

    TAREFAS[task_id]["status"] = "processando"
    TAREFAS[task_id]["progresso"] = 5
    TAREFAS[task_id]["mensagem"] = "Padronizando proporção vertical 9:16 no servidor..."

    try:
        def padronizar_9_16(video_in: Path, tag: str) -> Path:
            video_out = norm_dir / f"norm_{tag}_{video_in.name}.mp4"
            cmd = [
                "ffmpeg", "-y", "-i", str(video_in),
                "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1",
                "-r", "30", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", str(video_out)
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return video_out

        norm_g = [padronizar_9_16(g, f"g{i}") for i, g in enumerate(ganchos)]
        norm_c = [padronizar_9_16(c, f"c{i}") for i, c in enumerate(corpos)]
        norm_t = [padronizar_9_16(t, f"t{i}") for i, t in enumerate(ctas)]

        TAREFAS[task_id]["progresso"] = 25
        TAREFAS[task_id]["mensagem"] = "Embaralhando permutações de vídeos..."

        combinacoes = []
        for g in norm_g:
            for c in norm_c:
                for t in norm_t:
                    combinacoes.append((g, c, t))

        random.shuffle(combinacoes)
        total_vids = len(combinacoes)
        finais = []

        for idx, (g, c, t) in enumerate(combinacoes):
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

            prog = 25 + int(((idx + 1) / total_vids) * 65)
            TAREFAS[task_id]["progresso"] = prog
            TAREFAS[task_id]["mensagem"] = f"Montando variação {idx+1} de {total_vids}..."

        zip_path = task_dir / f"{prefixo}_Lote_Multiplicado.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for f in finais:
                z.write(f, arcname=f.name)

        shutil.rmtree(norm_dir, ignore_errors=True)
        shutil.rmtree(task_dir / "inputs", ignore_errors=True)

        TAREFAS[task_id]["status"] = "concluido"
        TAREFAS[task_id]["progresso"] = 100
        TAREFAS[task_id]["mensagem"] = f"{total_vids} vídeos renderizados e empacotados com sucesso!"
        TAREFAS[task_id]["download_url"] = f"/api/download/{task_id}"

    except Exception as e:
        TAREFAS[task_id]["status"] = "erro"
        TAREFAS[task_id]["mensagem"] = f"Falha na multiplicação: {str(e)}"

# ============================================================================
# ROTAS DA API (TODAS AS ROTAS COMPLETAS)
# ============================================================================
@app.get("/")
def index():
    idx_path = BASE_DIR / "index.html"
    if idx_path.exists():
        return FileResponse(idx_path)
    return {"status": "+VIDEOS API Online"}

@app.post("/api/roteiro")
async def rota_roteiro(payload: Dict[str, str]):
    produto = payload.get("produto", "").strip()
    if not produto:
        raise HTTPException(status_code=400, detail="Nome do produto obrigatório")

    if not GROQ_API_KEY:
        return fallback_roteiro(produto)

    url_groq = "https://api.groq.com/openai/v1/chat/completions"
    headers_groq = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }

    prompt_sistema = """Você é um copywriter de elite especializado em TikTok Shop Brasil.
Crie um roteiro em 3 partes curtas:
1. Gancho (0 a 3s): Quebra de padrão impactante ou curiosidade irresistível.
2. Corpo (3 a 25s): Demonstração visual rápida da solução quebrando a principal objeção.
3. CTA (Final): Chamada com urgência para a sacola do TikTok Shop.

Responda EXCLUSIVAMENTE em formato JSON:
{"gancho": "...", "corpo": "...", "cta": "..."}"""

    body_groq = {
        "model": "llama-3.3-70b-versatile",
        "messages": [
            {"role": "system", "content": prompt_sistema},
            {"role": "user", "content": f"Produto: '{produto}' (Crie uma variação inovadora e agressiva)"}
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.85
    }

    try:
        resp = requests.post(url_groq, headers=headers_groq, json=body_groq, timeout=8)
        if resp.status_code == 200:
            dados = resp.json()
            conteudo = dados["choices"][0]["message"]["content"]
            return json.loads(conteudo)
        return fallback_roteiro(produto)
    except Exception:
        return fallback_roteiro(produto)

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
    for f in arquivos:
        dest = in_dir / f.filename
        with open(dest, "wb") as b:
            shutil.copyfileobj(f.file, b)
        salvos.append(dest)

    TAREFAS[task_id] = {
        "id": task_id,
        "status": "pendente",
        "progresso": 0,
        "mensagem": "Na fila do servidor...",
        "download_url": None
    }

    background_tasks.add_task(task_remover_pausas, task_id, salvos, prefixo, sensibilidade)
    return {"task_id": task_id}

@app.post("/api/multiplicar")
async def rota_multiplicar(
    background_tasks: BackgroundTasks,
    ganchos: List[UploadFile] = File(...),
    corpos: List[UploadFile] = File(...),
    ctas: List[UploadFile] = File(...),
    prefixo: str = Form("Meu_Video")
):
    task_id = str(uuid.uuid4())
    in_dir = TEMP_DIR / task_id / "inputs"
    in_dir.mkdir(parents=True, exist_ok=True)

    def salvar_lote(arqs, sub):
        dest_dir = in_dir / sub
        dest_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for f in arqs:
            p = dest_dir / f.filename
            with open(p, "wb") as b:
                shutil.copyfileobj(f.file, b)
            paths.append(p)
        return paths

    g_paths = salvar_lote(ganchos, "g")
    c_paths = salvar_lote(corpos, "c")
    t_paths = salvar_lote(ctas, "t")

    TAREFAS[task_id] = {
        "id": task_id,
        "status": "pendente",
        "progresso": 0,
        "mensagem": "Iniciando renderização em nuvem...",
        "download_url": None
    }

    background_tasks.add_task(task_multiplicar_videos, task_id, g_paths, c_paths, t_paths, prefixo)
    return {"task_id": task_id}

@app.get("/api/tarefa/{task_id}")
async def checar_status_tarefa(task_id: str):
    if task_id not in TAREFAS:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
    return TAREFAS[task_id]

@app.get("/api/download/{task_id}")
async def download_zip(task_id: str):
    task_dir = TEMP_DIR / task_id
    zips = list(task_dir.glob("*.zip"))
    if not zips:
        raise HTTPException(status_code=404, detail="Arquivo ZIP não encontrado ou expirou")
    return FileResponse(zips[0], filename=zips[0].name, media_type="application/zip")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
