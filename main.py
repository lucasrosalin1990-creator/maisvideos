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

app = FastAPI(title="+VIDEOS Cloud Micro SaaS", version="2.3.0")

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

TAREFAS_DIR = TEMP_DIR / "tarefas_json"
TAREFAS_DIR.mkdir(exist_ok=True)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()

def salvar_tarefa(task_id: str, dados: dict):
    path = TAREFAS_DIR / f"{task_id}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False)

def carregar_tarefa(task_id: str) -> dict:
    path = TAREFAS_DIR / f"{task_id}.json"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None

# ============================================================================
# PROCESSAMENTO FFmpeg 1: REMOVEDOR REAL DE PAUSAS
# ============================================================================
def task_remover_pausas(task_id: str, caminhos_videos: List[Path], prefixo: str, sensibilidade: str):
    task_dir = TEMP_DIR / task_id
    out_dir = task_dir / "limpos"
    out_dir.mkdir(parents=True, exist_ok=True)

    tarefa_info = {
        "id": task_id,
        "status": "processando",
        "progresso": 15,
        "mensagem": "Analisando frequências de áudio com FFmpeg...",
        "download_url": None
    }
    salvar_tarefa(task_id, tarefa_info)

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
            tarefa_info["progresso"] = prog
            tarefa_info["mensagem"] = f"Cortando pausas do vídeo {idx+1} de {total}..."
            salvar_tarefa(task_id, tarefa_info)

        zip_path = task_dir / f"{prefixo}_Videos_Limpos.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for v in videos_processados:
                z.write(v, arcname=v.name)

        shutil.rmtree(task_dir / "inputs", ignore_errors=True)

        tarefa_info["status"] = "concluido"
        tarefa_info["progresso"] = 100
        tarefa_info["mensagem"] = f"Sucesso! {total} vídeos limpos na nuvem."
        tarefa_info["download_url"] = f"/api/download/{task_id}"
        salvar_tarefa(task_id, tarefa_info)

    except Exception as e:
        tarefa_info["status"] = "erro"
        tarefa_info["mensagem"] = f"Falha no processamento: {str(e)}"
        salvar_tarefa(task_id, tarefa_info)

# ============================================================================
# PROCESSAMENTO FFmpeg 2: MULTIPLICADOR 9:16 COM SHUFFLE
# ============================================================================
def task_multiplicar_videos(task_id: str, ganchos: List[Path], corpos: List[Path], ctas: List[Path], prefixo: str):
    task_dir = TEMP_DIR / task_id
    out_dir = task_dir / "criativos"
    norm_dir = task_dir / "norm"
    out_dir.mkdir(parents=True, exist_ok=True)
    norm_dir.mkdir(parents=True, exist_ok=True)

    tarefa_info = {
        "id": task_id,
        "status": "processando",
        "progresso": 5,
        "mensagem": "Padronizando proporção vertical 9:16 no servidor...",
        "download_url": None
    }
    salvar_tarefa(task_id, tarefa_info)

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

        norm_g = [padronizar_9_1
