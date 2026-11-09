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

# LÊ A CHAVE DO GOOGLE GEMINI DAS VARIÁVEIS DE AMBIENTE DO RENDER
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# GERENCIADOR DE TAREFAS NA NUVEM
TAREFAS: Dict[str, Dict[str, Any]] = {}

# RESOLVE LINKS ENCURTADOS (VT.TIKTOK.COM) E EXTRAI O NOME REAL DO PRODUTO
def resolver_nome_produto(input_texto: str) -> str:
    texto = input_texto.strip()
    urls = re.findall(r'(https?://[^\s]+)', texto)
    if not urls:
        return texto

    url = urls[0]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=6, allow_redirects=True)
        html = resp.text
        
        og_title = re.search(r'<meta property="og:title" content="([^"]+)"', html, re.IGNORECASE)
        if og_title:
            titulo = og_title.group(1).strip()
            titulo = re.sub(r'\s*\|\s*TikTok.*', '', titulo, flags=re.IGNORECASE)
            titulo = re.sub(r'\s*no TikTok.*', '', titulo, flags=re.IGNORECASE)
            if len(titulo) > 3:
                return titulo[:65]

        title_tag = re.search(r'<title>([^<]+)</title>', html, re.IGNORECASE)
        if title_tag:
            titulo = title_tag.group(1).strip()
            titulo = re.sub(r'\s*\|\s*TikTok.*', '', titulo, flags=re.IGNORECASE)
            if len(titulo) > 3:
                return titulo[:65]
    except Exception as e:
        print(f"Erro ao resolver URL do TikTok: {e}")

    return texto

# COMUNICAÇÃO REAL COM A API DO GOOGLE GEMINI (MODELO GEMINI-3.8-FLASH)
def chamar_gemini_api(produto_nome: str, estilo: str = "") -> dict:
    if not GEMINI_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="A variável GEMINI_API_KEY não foi configurada nas Environment Variables do Render."
        )

    # MODELO ATUALIZADO EXATO DO GOOGLE GEMINI
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent?key={GEMINI_API_KEY}"
    
    headers_gemini = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY
    }

    instrucao_tom = f"Estilo/Tom solicitado: '{estilo}'." if estilo else "Crie um roteiro de alta conversão, agressivo e persuasivo."

    prompt = f"""Você é um copywriter profissional de elite para TikTok Shop Brasil.
Crie um roteiro viral e irresistível em 3 partes para o produto: '{produto_nome}'.
{instrucao_tom}

Estrutura obrigatória:
1. Gancho (0 a 3s): Quebra de padrão impactante, dor extrema ou curiosidade irresistível.
2. Corpo (3 a 25s): Demonstração visual rápida da solução quebrando a principal objeção de compra.
3. CTA (Final): Chamada urgente para clicar na sacola/carrinho do TikTok Shop.

Responda EXCLUSIVAMENTE em formato JSON puro, sem markdown, sem crases, sem texto antes ou depois:
{{"gancho": "...", "corpo": "...", "cta": "..."}}"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.85,
            "responseMimeType": "application/json"
        }
    }

    resp = requests.post(url, headers=headers_gemini, json=payload, timeout=12)
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=f"Erro da API do Gemini: {resp.text}")

    dados = resp.json()
    try:
        texto_json = dados["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(texto_json)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Falha ao interpretar JSON do Gemini: {str(e)}")

# ============================================================================
# PROCESSAMENTO FFmpeg 1: REMOVEDOR DE PAUSAS
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
# PROCESSAMENTO FFmpeg 2: MULTIPLICADOR 9:16 COM EMBARALHAMENTO
# ============================================================================
def task_multiplicar_videos(task_id: str, ganchos: List[Path], corpos: List[Path], ctas: List[Path], prefixo: str):
    task_dir = TEMP_DIR / task_id
    out_dir = task_dir / "criativos"
    norm_dir = task_dir / "norm"
    out_dir.mkdir(parents=True, exist_ok=True)
    norm_dir.mkdir(parents=True, exist_ok=True)

    TAREFAS[task_id]["status"] = "processando"
    TAREFAS[task_id]["progresso"] = 10
    TAREFAS[task_id]["mensagem"] = "Padronizando blocos para formato vertical 9:16..."

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

        TAREFAS[task_id]["progresso"] = 30
        TAREFAS[task_id]["mensagem"] = "Embaralhando permutações de vídeos..."

        combinacoes = []
        for g in norm_g:
            for c in norm_c:
                for t in norm_t:
                    combinacoes.append((g, c, t))

        # Embaralhamento Fisher-Yates
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

            prog = 30 + int(((idx + 1) / total_vids) * 60)
            TAREFAS[task_id]["progresso"] = prog
            TAREFAS[task_id]["mensagem"] = f"Renderizando variação {idx+1} de {total_vids}..."

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
# ROTAS DA API (PÁGINA, VÍDEO TUTORIAL, IA E PROCESSAMENTO)
# ============================================================================

# ROTA 1: PÁGINA INICIAL
@app.get("/")
def index():
    idx_path = BASE_DIR / "index.html"
    if idx_path.exists():
        return FileResponse(idx_path)
    return {"status": "+VIDEOS Cloud Online"}

# ROTA 2: VÍDEO TUTORIAL.MP4 (NUNCA MAIS DÁ NOT FOUND)
@app.get("/tutorial.mp4")
def get_tutorial_video():
    vid_path = BASE_DIR / "tutorial.mp4"
    if vid_path.exists():
        return FileResponse(vid_path, media_type="video/mp4")
    raise HTTPException(status_code=404, detail="tutorial.mp4 não encontrado no servidor")

# ROTA 3: GERADOR DE ROTEIRO COM IA GEMINI
@app.post("/api/roteiro")
async def rota_roteiro(payload: Dict[str, Any]):
    raw_input = payload.get("produto", "").strip()
    estilo = payload.get("estilo", "").strip()
    if not raw_input:
        raise HTTPException(status_code=400, detail="Nome ou link do produto é obrigatório")

    produto_nome = resolver_nome_produto(raw_input)
    roteiro = chamar_gemini_api(produto_nome, estilo)
    return {
        "produto_resolvido": produto_nome,
        "gancho": roteiro.get("gancho", ""),
        "corpo": roteiro.get("corpo", ""),
        "cta": roteiro.get("cta", "")
    }

# ROTA 4: REMOVEDOR DE PAUSAS
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

# ROTA 5: MULTIPLICADOR DE VÍDEOS
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
        "mensagem": "Iniciando renderização na nuvem...",
        "download_url": None
    }

    background_tasks.add_task(task_multiplicar_videos, task_id, g_paths, c_paths, t_paths, prefixo)
    return {"task_id": task_id}

# ROTA 6: MONITORAMENTO DE PROGRESSO
@app.get("/api/tarefa/{task_id}")
async def checar_status_tarefa(task_id: str):
    if task_id not in TAREFAS:
        raise HTTPException(status_code=404, detail="Tarefa não encontrada")
    return TAREFAS[task_id]

# ROTA 7: DOWNLOAD DO PACOTE .ZIP
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
