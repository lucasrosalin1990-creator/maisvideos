from fastapi import Request
import os

import re

import uuid

import random

import shutil

import zipfile

import subprocess
from datetime import datetime, timezone

from typing import List, Dict, Any

from pathlib import Path

from fastapi import (

    FastAPI, UploadFile, File, Form,

    BackgroundTasks, HTTPException, Depends

)

from fastapi.responses import FileResponse

from fastapi.middleware.cors import CORSMiddleware

from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from dotenv import load_dotenv

from supabase import create_client

from pydantic import BaseModel

# CONFIGURAÇÃO DO SUPABASE

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(dotenv_path=BASE_DIR / ".env")

SUPABASE_URL = os.getenv("SUPABASE_URL")

SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")

SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "").strip().lower()
if not SUPABASE_URL or not SUPABASE_ANON_KEY or not SUPABASE_SERVICE_ROLE_KEY:

    raise RuntimeError("Configuração do Supabase incompleta.")

supabase_admin = create_client(

    SUPABASE_URL,

    SUPABASE_SERVICE_ROLE_KEY

)

class CadastroRequest(BaseModel):

    email: str

    senha: str

class LoginRequest(BaseModel):

    email: str

    senha: str

bearer_scheme = HTTPBearer(auto_error=False)

def obter_usuario_atual(

    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)

):

    if credentials is None:

        raise HTTPException(

            status_code=401,

            detail="Autenticação necessária."

        )

    token = credentials.credentials

    try:

        resposta = supabase_admin.auth.get_user(token)

        usuario = resposta.user

        if not usuario:

            raise HTTPException(

                status_code=401,

                detail="Usuário não autenticado."

            )

        return {

            "id": str(usuario.id),

            "email": usuario.email

        }

    except HTTPException:

        raise

    except Exception:

        raise HTTPException(

            status_code=401,

            detail="Token inválido ou expirado."

        )

def verificar_assinatura(user_id: str):

    try:

        resposta = (

            supabase_admin.table("assinaturas")

            .select("id, plano, status, vence_em")

            .eq("user_id", user_id)

            .eq("status", "ativa")

            .order("vence_em", desc=True)

            .limit(1)

            .execute()

        )

        if not resposta.data:

            raise HTTPException(

                status_code=403,

                detail="Você não possui uma assinatura ativa."

            )

        assinatura = resposta.data[0]

        vencimento = assinatura.get("vence_em")
        if not vencimento:
            raise HTTPException(
                status_code=403,
                detail="A validade da sua assinatura não está confirmada."
            )

        data_vencimento = datetime.fromisoformat(
            vencimento.replace("Z", "+00:00")
        )
        if data_vencimento.tzinfo is None:
            data_vencimento = data_vencimento.replace(tzinfo=timezone.utc)

        if data_vencimento <= datetime.now(timezone.utc):
            raise HTTPException(
                status_code=403,
                detail="Sua assinatura expirou."
            )

        return assinatura

    except HTTPException:

        raise

    except Exception:

        raise HTTPException(

            status_code=503,

            detail="Não foi possível verificar sua assinatura."

        )

from fastapi.staticfiles import StaticFiles

app = FastAPI(title="+VIDEOS Cloud Engine", version="3.0.0")

# Arquivos do motor de processamento local (FFmpeg.wasm)
app.mount("/ffmpeg", StaticFiles(directory=str(Path(__file__).resolve().parent / "ffmpeg")), name="ffmpeg")

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

# Limites configuráveis para evitar uploads excessivos.
MAX_UPLOAD_MB = max(1, int(os.getenv("MAX_UPLOAD_MB", "500")))
MAX_TOTAL_UPLOAD_MB = max(MAX_UPLOAD_MB, int(os.getenv("MAX_TOTAL_UPLOAD_MB", "2000")))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_TOTAL_UPLOAD_BYTES = MAX_TOTAL_UPLOAD_MB * 1024 * 1024
EXTENSOES_VIDEO_PERMITIDAS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
TAMANHO_BLOCO_UPLOAD = 1024 * 1024

@app.post("/api/auth/login")
def login_usuario(dados: LoginRequest):
    email = dados.email.strip().lower()

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=400, detail="Informe um e-mail válido.")

    try:
        cliente = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
        resposta = cliente.auth.sign_in_with_password({
            "email": email,
            "password": dados.senha
        })

        if not resposta.user or not resposta.session:
            raise HTTPException(
                status_code=401,
                detail="E-mail ou senha inválidos."
            )

        usuario = resposta.user
        if not usuario.email_confirmed_at:
            raise HTTPException(
                status_code=403,
                detail="Confirme seu e-mail antes de entrar."
            )

        # Vincula compras aprovadas que ainda não foram associadas a uma conta.
        supabase_admin.table("compras_pendentes").update({
            "user_id": str(usuario.id),
            "vinculada_em": datetime.now(timezone.utc).isoformat()
        }).eq("email", email).eq("status", "aprovada").is_(
            "user_id", "null"
        ).execute()

        _mv_vincular_compras_e_ativar_assinaturas(email, str(usuario.id))

        return {
            "ok": True,
            "access_token": resposta.session.access_token,
            "token_type": "bearer",
            "usuario": {
                "id": str(usuario.id),
                "email": usuario.email
            }
        }

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=401,
            detail="Não foi possível entrar. Verifique seu e-mail e senha."
        )

# GERENCIADOR DE TAREFAS NA NUVEM

TAREFAS: Dict[str, Dict[str, Any]] = {}

def usuario_com_assinatura(
    usuario: Dict[str, str] = Depends(obter_usuario_atual)
):
    email_usuario = (usuario.get("email") or "").strip().lower()

    # Administrador com acesso permanente
    if ADMIN_EMAIL and email_usuario == ADMIN_EMAIL:
        return usuario

    # Clientes continuam precisando de assinatura ativa
    verificar_assinatura(usuario["id"])

    return usuario


@app.get("/api/auth/verificar-assinatura")
def verificar_acesso_local(
    usuario: Dict[str, str] = Depends(usuario_com_assinatura)
):
    return {"ok": True}


def exigir_dono_tarefa(task_id: str, usuario: Dict[str, str]):

    validar_task_id(task_id)

    tarefa = TAREFAS.get(task_id)

    if not tarefa:

        raise HTTPException(

            status_code=404,

            detail="Tarefa não encontrada."

        )

    if tarefa.get("user_id") != usuario["id"]:

        raise HTTPException(

            status_code=404,

            detail="Tarefa não encontrada."

        )

    return tarefa

def obter_duracao(caminho: Path) -> float:
    """Retorna a duração real do vídeo; falha se o arquivo não for válido."""
    if not caminho.is_file() or caminho.stat().st_size <= 0:
        raise RuntimeError(f"Arquivo de vídeo ausente ou vazio: {caminho.name}")

    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(caminho)
    ]
    try:
        res = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, check=True, timeout=120
        )
        duracao = float(res.stdout.strip())
    except FileNotFoundError as exc:
        raise RuntimeError("FFprobe não foi encontrado no servidor.") from exc
    except (subprocess.SubprocessError, ValueError) as exc:
        detalhe = getattr(exc, "stderr", None)
        detalhe = (detalhe or "").strip()[-500:]
        raise RuntimeError(f"Não foi possível validar o vídeo {caminho.name}. {detalhe}") from exc

    if duracao <= 0:
        raise RuntimeError(f"O vídeo {caminho.name} tem duração inválida.")
    return duracao


def executar_ffmpeg(cmd: List[str], descricao: str, timeout: int = 3600):
    """Executa FFmpeg e transforma falhas do processo em erro explícito."""
    try:
        resultado = subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            text=True, check=True, timeout=timeout
        )
        return resultado
    except FileNotFoundError as exc:
        raise RuntimeError("FFmpeg não foi encontrado no servidor.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Tempo limite excedido: {descricao}.") from exc
    except subprocess.CalledProcessError as exc:
        detalhe = (exc.stderr or "").strip()[-700:]
        raise RuntimeError(f"Falha ao {descricao}. {detalhe}") from exc


def validar_saida_video(caminho: Path) -> float:
    """Confirma que a saída existe, não está vazia e pode ser lida pelo ffprobe."""
    return obter_duracao(caminho)


async def salvar_upload_video(upload: UploadFile, destino: Path, total_recebido: List[int]) -> Path:
    """Salva upload em blocos, validando extensão e limites por arquivo e por tarefa."""
    nome_original = upload.filename or ""
    extensao = Path(nome_original.replace("\\", "/")).suffix.lower()
    if extensao not in EXTENSOES_VIDEO_PERMITIDAS:
        raise HTTPException(
            status_code=415,
            detail=f"Formato não permitido: {extensao or 'arquivo sem extensão'}. Envie MP4, MOV, M4V, WEBM, AVI ou MKV."
        )

    tamanho = 0
    try:
        with destino.open("wb") as arquivo_saida:
            while True:
                bloco = await upload.read(TAMANHO_BLOCO_UPLOAD)
                if not bloco:
                    break
                tamanho += len(bloco)
                total_recebido[0] += len(bloco)

                if tamanho > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"O arquivo {Path(nome_original).name} ultrapassa o limite de {MAX_UPLOAD_MB} MB."
                    )
                if total_recebido[0] > MAX_TOTAL_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"O conjunto de arquivos ultrapassa o limite de {MAX_TOTAL_UPLOAD_MB} MB por processamento."
                    )
                arquivo_saida.write(bloco)

        if tamanho <= 0:
            raise HTTPException(status_code=400, detail=f"O arquivo {Path(nome_original).name} está vazio.")
        return destino
    except Exception:
        destino.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()

def validar_task_id(task_id: str) -> str:

    try:

        if str(uuid.UUID(task_id)) != task_id:

            raise ValueError

    except (ValueError, AttributeError):

        raise HTTPException(status_code=404, detail="Tarefa não encontrada")

    return task_id

def caminho_da_tarefa(task_id: str) -> Path:

    validar_task_id(task_id)

    raiz = TEMP_DIR.resolve()

    tarefa = raiz / task_id

    if tarefa.is_symlink():

        raise HTTPException(status_code=404, detail="Tarefa não encontrada")

    tarefa_resolvida = tarefa.resolve()

    if tarefa_resolvida.parent != raiz or not tarefa_resolvida.is_dir():

        raise HTTPException(status_code=404, detail="Tarefa não encontrada")

    return tarefa_resolvida

def limpar_prefixo(prefixo: str) -> str:

    nome = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]+', "_", prefixo or "").strip()

    return (nome or "Meu_Video")[:100]

def detectar_limiar_inteligente(caminho: Path, sensibilidade: str) -> tuple:

    """Mede o volume médio real do vídeo para calibrar o corte perfeitamente em iPhones e celulares."""

    cmd_vol = ["ffmpeg", "-y", "-i", str(caminho), "-af", "volumedetect", "-f", "null", "-"]

    res = executar_ffmpeg(cmd_vol, "analisar o volume do vídeo", timeout=600)

    match = re.search(r"mean_volume:\s*(-?\d+\.?\d*)\s*dB", res.stderr or "")

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

    res_detect = executar_ffmpeg(cmd_detect, "detectar as pausas do vídeo", timeout=600)

    stderr = res_detect.stderr or ""

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

    executar_ffmpeg(cmd_cut, "gerar o vídeo sem pausas", timeout=3600)

    dur_depois = validar_saida_video(video_out)

    return dur_antes, dur_depois

def padronizar_apenas_9_16(video_in: Path, video_out: Path):

    """Padroniza para 9:16 (1080x1920) sem cortar nenhuma pausa."""

    cmd = [

        "ffmpeg", "-y", "-i", str(video_in),

        "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1",

        "-r", "30", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",

        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", str(video_out)

    ]

    executar_ffmpeg(cmd, "padronizar o vídeo para 9:16", timeout=3600)
    validar_saida_video(video_out)

# ============================================================================

# TAREFA 1: REMOVEDOR DE PAUSAS NO VÍDEO

# ============================================================================

def task_remover_pausas(task_id: str, caminhos_videos: List[Path], prefixo: str, sensibilidade: str):

    task_dir = TEMP_DIR / task_id

    out_dir = task_dir / "outputs"

    out_dir.mkdir(parents=True, exist_ok=True)

    if TAREFAS[task_id].get("cancelado"):
        return

    TAREFAS[task_id]["status"] = "processando"

    TAREFAS[task_id]["progresso"] = 10

    TAREFAS[task_id]["mensagem"] = "Calibrando decibéis e removendo pausas..."

    arquivos_prontos = []

    lista_metadados = []

    total = len(caminhos_videos)

    try:
        if total == 0:
            raise RuntimeError("Nenhum vídeo foi recebido para processamento.")
        if sensibilidade not in {"suave", "equilibrado", "agressivo"}:
            raise RuntimeError("Sensibilidade de remoção de pausas inválida.")

        for idx, video_in in enumerate(caminhos_videos):

            if TAREFAS[task_id].get("cancelado"):

                return

            nome_saida = f"{limpar_prefixo(prefixo)}_{idx+1:02d}.mp4"

            video_out = out_dir / nome_saida

            dur_antes, dur_depois = cortar_pausas_ffmpeg(video_in, video_out, sensibilidade, pad_9_16=False)
            validar_saida_video(video_out)
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

        if TAREFAS[task_id].get("cancelado"):
            return
        if not arquivos_prontos:
            raise RuntimeError("Nenhum vídeo válido foi gerado.")

        zip_path = task_dir / f"{limpar_prefixo(prefixo)}_Videos_Sem_Pausa.zip"

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:

            for v in arquivos_prontos:

                z.write(v, arcname=v.name)

        with zipfile.ZipFile(zip_path, "r") as zip_teste:
            if zip_teste.testzip() is not None:
                raise RuntimeError("O arquivo ZIP gerado apresentou erro de integridade.")

        shutil.rmtree(task_dir / "inputs", ignore_errors=True)

        TAREFAS[task_id]["status"] = "concluido"

        TAREFAS[task_id]["progresso"] = 100

        TAREFAS[task_id]["mensagem"] = f"Concluído! {total} vídeo(s) processado(s) sem pausas."

        TAREFAS[task_id]["download_url"] = f"/api/download/{task_id}"

        TAREFAS[task_id]["arquivos"] = lista_metadados

    except Exception as e:

        if not TAREFAS[task_id].get("cancelado"):
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

    if TAREFAS[task_id].get("cancelado"):
        return

    TAREFAS[task_id]["status"] = "processando"

    TAREFAS[task_id]["progresso"] = 5

    TAREFAS[task_id]["mensagem"] = (

        "Removendo pausas de todos os blocos e padronizando 9:16..."

        if remover_pausas else

        "Padronizando Ganchos, Corpos e CTAs em formato 9:16..."

    )

    try:
        if not ganchos or not corpos or not ctas:
            raise RuntimeError("Envie pelo menos um vídeo em cada bloco: Ganchos, Corpos e CTAs.")
        if remover_pausas and sensibilidade not in {"suave", "equilibrado", "agressivo"}:
            raise RuntimeError("Sensibilidade de remoção de pausas inválida.")

        total_blocos = len(ganchos) + len(corpos) + len(ctas)

        blocos_processados = 0

        def preparar_bloco(caminho_in: Path, tag: str) -> Path:

            nonlocal blocos_processados

            saida = norm_dir / f"prep_{tag}.mp4"

            if remover_pausas:

                cortar_pausas_ffmpeg(caminho_in, saida, sensibilidade, pad_9_16=True)

            else:

                padronizar_apenas_9_16(caminho_in, saida)

            validar_saida_video(saida)
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
        if total_vids <= 0:
            raise RuntimeError("Não foi possível formar combinações. Confira os três blocos.")

        finais = []

        lista_metadados = []

        for idx, (g, c, t) in enumerate(combinacoes):

            if TAREFAS[task_id].get("cancelado"):

                return

            nome_arq = f"{limpar_prefixo(prefixo)}_{idx+1:02d}.mp4"

            criativo_path = out_dir / nome_arq

            concat_txt = task_dir / f"list_{idx}.txt"

            with open(concat_txt, "w", encoding="utf-8") as f:

                f.write(f"file '{g.resolve()}'\nfile '{c.resolve()}'\nfile '{t.resolve()}'\n")

            cmd_concat = [

                "ffmpeg", "-y", "-f", "concat", "-safe", "0",

                "-i", str(concat_txt),

                "-c", "copy", str(criativo_path)

            ]

            try:
                executar_ffmpeg(cmd_concat, f"montar a combinação {idx + 1}", timeout=1800)
            finally:
                concat_txt.unlink(missing_ok=True)

            dur_final = validar_saida_video(criativo_path)
            finais.append(criativo_path)

            lista_metadados.append({

                "nome": nome_arq,

                "url": f"/api/arquivo/{task_id}/{nome_arq}",

                "info": f"Duração: {dur_final:.1f}s • 9:16"

            })

            prog = 42 + int(((idx + 1) / total_vids) * 50)

            TAREFAS[task_id]["progresso"] = prog

            TAREFAS[task_id]["mensagem"] = f"Montando vídeo {idx+1} de {total_vids}..."

        if TAREFAS[task_id].get("cancelado"):
            return
        if not finais:
            raise RuntimeError("Nenhum vídeo final foi gerado.")

        zip_path = task_dir / f"{limpar_prefixo(prefixo)}_Lote_Multiplicado.zip"

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:

            for f in finais:

                z.write(f, arcname=f.name)

        with zipfile.ZipFile(zip_path, "r") as zip_teste:
            if zip_teste.testzip() is not None:
                raise RuntimeError("O arquivo ZIP gerado apresentou erro de integridade.")

        shutil.rmtree(norm_dir, ignore_errors=True)

        shutil.rmtree(task_dir / "inputs", ignore_errors=True)

        TAREFAS[task_id]["status"] = "concluido"

        TAREFAS[task_id]["progresso"] = 100

        TAREFAS[task_id]["mensagem"] = f"{total_vids} vídeos gerados e embaralhados com sucesso!"

        TAREFAS[task_id]["download_url"] = f"/api/download/{task_id}"

        TAREFAS[task_id]["arquivos"] = lista_metadados

    except Exception as e:

        if not TAREFAS[task_id].get("cancelado"):
            TAREFAS[task_id]["status"] = "erro"
            TAREFAS[task_id]["mensagem"] = f"Erro na multiplicação: {str(e)}"

# ============================================================================

# ROTAS DA API

# ============================================================================

@app.post("/api/auth/cadastro")
def cadastrar_usuario(dados: CadastroRequest):
    email = dados.email.strip().lower()
    senha = dados.senha

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=400, detail="Informe um e-mail válido.")

    if len(senha) < 8:
        raise HTTPException(
            status_code=400,
            detail="A senha precisa ter pelo menos 8 caracteres."
        )

    # A compra deve estar aprovada e ainda não vinculada a outra conta.
    try:
        compra = (
            supabase_admin.table("compras_pendentes")
            .select("compra_id")
            .eq("email", email)
            .eq("status", "aprovada")
            .is_("user_id", "null")
            .limit(1)
            .execute()
        )
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Não foi possível verificar a compra."
        )

    if not compra.data:
        raise HTTPException(
            status_code=403,
            detail="Não encontramos uma compra aprovada e disponível para este e-mail."
        )

    try:
        cliente = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
        resposta = cliente.auth.sign_up({
            "email": email,
            "password": senha
        })

        if not resposta.user:
            raise HTTPException(
                status_code=400,
                detail="Não foi possível iniciar o cadastro."
            )

        supabase_admin.table("compras_pendentes").update({
            "user_id": str(resposta.user.id),
            "vinculada_em": datetime.now(timezone.utc).isoformat()
        }).eq("email", email).eq("status", "aprovada").is_(
            "user_id", "null"
        ).execute()

        _mv_vincular_compras_e_ativar_assinaturas(email, str(resposta.user.id))

        return {
            "ok": True,
            "mensagem": (
                "Cadastro iniciado. Verifique seu e-mail "
                "para confirmar sua conta."
            )
        }

    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Não foi possível concluir o cadastro. Verifique o e-mail ou tente entrar."
        )

@app.get("/vendas")
def pagina_vendas():
    return FileResponse(BASE_DIR / "vendas.html")

@app.get("/")

def index():

    idx_path = BASE_DIR / "index.html"

    if idx_path.exists():

        return FileResponse(idx_path)

    return {"status": "+VIDEOS Cloud Online"}

@app.post("/api/remover-pausas")
async def rota_remover_pausas(
    background_tasks: BackgroundTasks,
    usuario: Dict[str, str] = Depends(usuario_com_assinatura),
    arquivos: List[UploadFile] = File(...),
    prefixo: str = Form("Meu_Video"),
    sensibilidade: str = Form("equilibrado")
):
    if not arquivos:
        raise HTTPException(status_code=400, detail="Selecione pelo menos um vídeo.")
    if sensibilidade not in {"suave", "equilibrado", "agressivo"}:
        raise HTTPException(status_code=400, detail="Sensibilidade inválida.")

    task_id = str(uuid.uuid4())
    task_dir = TEMP_DIR / task_id
    in_dir = task_dir / "inputs"
    in_dir.mkdir(parents=True, exist_ok=True)
    salvos = []
    total_recebido = [0]

    try:
        for idx, arquivo in enumerate(arquivos):
            destino = in_dir / f"raw_{idx}{Path(arquivo.filename or '').suffix.lower()}"
            salvos.append(await salvar_upload_video(arquivo, destino, total_recebido))
    except Exception:
        shutil.rmtree(task_dir, ignore_errors=True)
        for arquivo in arquivos:
            if not arquivo.file.closed:
                await arquivo.close()
        raise

    TAREFAS[task_id] = {
        "id": task_id, "user_id": usuario["id"], "status": "pendente",
        "progresso": 5, "mensagem": "Iniciando corte no servidor...",
        "download_url": None, "arquivos": [], "cancelado": False
    }
    background_tasks.add_task(
        task_remover_pausas, task_id, salvos, limpar_prefixo(prefixo), sensibilidade
    )
    return {"task_id": task_id}


@app.post("/api/multiplicar")
async def rota_multiplicar(
    background_tasks: BackgroundTasks,
    usuario: Dict[str, str] = Depends(usuario_com_assinatura),
    ganchos: List[UploadFile] = File(...),
    corpos: List[UploadFile] = File(...),
    ctas: List[UploadFile] = File(...),
    prefixo: str = Form("Meu_Video"),
    remover_pausas: str = Form("false"),
    sensibilidade: str = Form("equilibrado")
):
    if not ganchos or not corpos or not ctas:
        raise HTTPException(
            status_code=400,
            detail="Envie pelo menos um vídeo em cada bloco: Ganchos, Corpos e CTAs."
        )
    cortar = remover_pausas.strip().lower() in {"true", "1", "sim", "yes"}
    if cortar and sensibilidade not in {"suave", "equilibrado", "agressivo"}:
        raise HTTPException(status_code=400, detail="Sensibilidade inválida.")

    task_id = str(uuid.uuid4())
    task_dir = TEMP_DIR / task_id
    in_dir = task_dir / "inputs"
    in_dir.mkdir(parents=True, exist_ok=True)
    total_recebido = [0]

    async def salvar_lote(uploads: List[UploadFile], sub: str) -> List[Path]:
        dest_dir = in_dir / sub
        dest_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for idx, upload in enumerate(uploads):
            destino = dest_dir / f"{sub}_{idx}{Path(upload.filename or '').suffix.lower()}"
            paths.append(await salvar_upload_video(upload, destino, total_recebido))
        return paths

    try:
        g_paths = await salvar_lote(ganchos, "g")
        c_paths = await salvar_lote(corpos, "c")
        t_paths = await salvar_lote(ctas, "t")
    except Exception:
        shutil.rmtree(task_dir, ignore_errors=True)
        for upload in [*ganchos, *corpos, *ctas]:
            if not upload.file.closed:
                await upload.close()
        raise

    TAREFAS[task_id] = {
        "id": task_id, "user_id": usuario["id"], "status": "pendente",
        "progresso": 5, "mensagem": "Iniciando processamento em nuvem...",
        "download_url": None, "arquivos": [], "cancelado": False
    }
    background_tasks.add_task(
        task_multiplicar_videos, task_id, g_paths, c_paths, t_paths,
        limpar_prefixo(prefixo), cortar, sensibilidade
    )
    return {"task_id": task_id}

@app.post("/api/cancelar/{task_id}")
async def cancelar_tarefa(
    task_id: str,
    usuario: Dict[str, str] = Depends(usuario_com_assinatura)
):
    tarefa = exigir_dono_tarefa(task_id, usuario)
    if tarefa.get("status") in {"concluido", "erro", "cancelado"}:
        return {"ok": False, "status": tarefa.get("status"), "mensagem": "A tarefa já foi finalizada."}
    tarefa["cancelado"] = True
    tarefa["status"] = "cancelado"
    tarefa["mensagem"] = "Processo interrompido pelo usuário."
    return {"ok": True}

@app.get("/api/tarefa/{task_id}")
async def checar_status_tarefa(
    task_id: str,
    usuario: Dict[str, str] = Depends(usuario_com_assinatura)
):
    return exigir_dono_tarefa(task_id, usuario)

@app.get("/api/arquivo/{task_id}/{filename}")
async def servir_video_individual(
    task_id: str,
    filename: str,
    usuario: Dict[str, str] = Depends(usuario_com_assinatura)
):
    tarefa = exigir_dono_tarefa(task_id, usuario)
    if tarefa.get("status") != "concluido":
        raise HTTPException(status_code=409, detail="O vídeo ainda não está disponível para download.")
    task_dir = caminho_da_tarefa(task_id)

    if (
        Path(filename).name != filename
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
        or not filename.lower().endswith(".mp4")
    ):
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")

    output_dir = task_dir / "outputs"
    if output_dir.is_symlink():
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")

    output_dir = output_dir.resolve()
    if output_dir.parent != task_dir:
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")

    candidate = output_dir / filename
    if candidate.is_symlink():
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")

    try:
        arq_path = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")

    if arq_path.parent != output_dir or not arq_path.is_file():
        raise HTTPException(status_code=404, detail="Vídeo não encontrado")

    return FileResponse(arq_path, filename=filename, media_type="video/mp4")

@app.get("/api/download/{task_id}")
async def download_zip(
    task_id: str,
    usuario: Dict[str, str] = Depends(usuario_com_assinatura)
):
    tarefa = exigir_dono_tarefa(task_id, usuario)
    if tarefa.get("status") != "concluido":
        raise HTTPException(status_code=409, detail="O arquivo ZIP ainda não está disponível.")
    task_dir = caminho_da_tarefa(task_id)

    for candidate in sorted(task_dir.glob("*.zip")):
        if candidate.is_symlink():
            continue

        try:
            zip_path = candidate.resolve(strict=True)
        except (OSError, RuntimeError):
            continue

        if zip_path.parent == task_dir and zip_path.is_file():
            return FileResponse(
                zip_path,
                filename=zip_path.name,
                media_type="application/zip"
            )

    raise HTTPException(status_code=404, detail="Arquivo ZIP não encontrado")

if __name__ == "__main__":

    import uvicorn

    port = int(os.environ.get("PORT", 10000))

    uvicorn.run("main:app", host="0.0.0.0", port=port)

# MAIS_VIDEOS_CAKTO_WEBHOOK_V1
# Integração Cakto -> Supabase. O vídeo continua sendo processado no navegador.
def _mv_data_iso(valor, padrao=None):
    from datetime import datetime, timezone
    if not valor:
        return padrao or datetime.now(timezone.utc)
    if isinstance(valor, datetime):
        data = valor
    else:
        data = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    if data.tzinfo is None:
        data = data.replace(tzinfo=timezone.utc)
    return data.astimezone(timezone.utc)


def _mv_somar_periodo(data_base, plano):
    import calendar
    meses = 1 if plano == "mensal" else 12
    indice = data_base.month - 1 + meses
    ano = data_base.year + indice // 12
    mes = indice % 12 + 1
    dia = min(data_base.day, calendar.monthrange(ano, mes)[1])
    return data_base.replace(year=ano, month=mes, day=dia)


def _mv_id_assinatura_cakto(pedido):
    assinatura = pedido.get("subscription")
    if isinstance(assinatura, dict):
        for chave in ("id", "subscription_id", "subscriptionId", "external_id"):
            valor = assinatura.get(chave)
            if valor:
                return str(valor)
    elif isinstance(assinatura, (str, int)) and assinatura:
        return str(assinatura)
    for chave in ("subscription_id", "subscriptionId", "cakto_subscription_id"):
        valor = pedido.get(chave)
        if valor:
            return str(valor)
    return None


def _mv_plano_cakto(pedido):
    oferta = pedido.get("offer") or {}
    produto = pedido.get("product") or {}
    id_oferta = str(oferta.get("id") or "").strip()
    id_produto = str(produto.get("id") or "").strip()

    mensal_oferta = os.getenv("CAKTO_MENSAL_OFFER_ID", "").strip()
    anual_oferta = os.getenv("CAKTO_ANUAL_OFFER_ID", "").strip()
    mensal_produto = os.getenv("CAKTO_MENSAL_PRODUCT_ID", "").strip()
    anual_produto = os.getenv("CAKTO_ANUAL_PRODUCT_ID", "").strip()

    # A oferta tem prioridade: permite mensal e anual dentro do mesmo produto.
    if mensal_oferta and id_oferta == mensal_oferta:
        return "mensal", "mais-videos-mensal"
    if anual_oferta and id_oferta == anual_oferta:
        return "anual", "mais-videos-anual"
    if mensal_produto and id_produto == mensal_produto:
        return "mensal", "mais-videos-mensal"
    if anual_produto and id_produto == anual_produto:
        return "anual", "mais-videos-anual"
    return None, None


def _mv_localizar_usuario_email(email):
    # Permite ativar automaticamente compras de quem já tem conta no Supabase,
    # mesmo que ainda não exista uma compra anterior vinculada.
    try:
        resposta = supabase_admin.auth.admin.list_users(page=1, per_page=1000)
    except TypeError:
        resposta = supabase_admin.auth.admin.list_users()
    usuarios = getattr(resposta, "users", resposta)
    for usuario in usuarios or []:
        if (getattr(usuario, "email", "") or "").strip().lower() == email:
            return str(usuario.id)
    return None


def _mv_vincular_compras_e_ativar_assinaturas(email, user_id):
    from datetime import datetime, timezone
    agora = datetime.now(timezone.utc)
    resposta = (
        supabase_admin.table("compras_pendentes")
        .select("compra_id,email,plano,status,cakto_subscription_id,dados_evento,produto_slug,user_id,criado_em")
        .eq("email", email)
        .eq("user_id", user_id)
        .eq("status", "aprovada")
        .order("criado_em")
        .execute()
    )

    for compra in resposta.data or []:
        compra_id = str(compra.get("compra_id") or "").strip()
        plano = str(compra.get("plano") or "").strip().lower()
        evento = compra.get("dados_evento") or {}
        if not compra_id or plano not in ("mensal", "anual"):
            continue

        # A chave compra_id é única. Se já está na assinatura, não renova de novo.
        por_compra = (
            supabase_admin.table("assinaturas")
            .select("id")
            .eq("compra_id", compra_id)
            .limit(1)
            .execute()
        )
        if por_compra.data:
            supabase_admin.table("compras_pendentes").update({
                "status": "vinculada",
                "atualizado_em": agora.isoformat(),
            }).eq("compra_id", compra_id).execute()
            continue

        assinatura_cakto_id = compra.get("cakto_subscription_id") or _mv_id_assinatura_cakto(evento)
        assinatura_existente = None
        if assinatura_cakto_id:
            resultado = (
                supabase_admin.table("assinaturas")
                .select("id,user_id,compra_id,plano,status,inicio_em,vence_em")
                .eq("cakto_subscription_id", str(assinatura_cakto_id))
                .limit(1)
                .execute()
            )
            if resultado.data:
                assinatura_existente = resultado.data[0]

        data_pagamento = _mv_data_iso(
            evento.get("paidAt") or evento.get("createdAt") or compra.get("criado_em"),
            agora,
        )
        meses = 1 if plano == "mensal" else 12
        inicio = data_pagamento
        base_vencimento = data_pagamento

        if assinatura_existente:
            inicio = _mv_data_iso(assinatura_existente.get("inicio_em"), data_pagamento)
            vencimento_atual = _mv_data_iso(assinatura_existente.get("vence_em"), data_pagamento)
            if vencimento_atual > base_vencimento:
                base_vencimento = vencimento_atual

        novo_vencimento = _mv_somar_periodo(base_vencimento, plano)
        dados_assinatura = {
            "user_id": user_id,
            "plano": plano,
            "status": "ativa",
            "compra_id": compra_id,
            "inicio_em": inicio.isoformat(),
            "vence_em": novo_vencimento.isoformat(),
            "atualizado_em": agora.isoformat(),
            "cakto_subscription_id": str(assinatura_cakto_id) if assinatura_cakto_id else None,
            "produto_slug": compra.get("produto_slug") or ("mais-videos-mensal" if plano == "mensal" else "mais-videos-anual"),
        }

        if assinatura_existente:
            supabase_admin.table("assinaturas").update(dados_assinatura).eq(
                "id", assinatura_existente["id"]
            ).execute()
        else:
            dados_assinatura["id"] = str(__import__("uuid").uuid4())
            dados_assinatura["criado_em"] = agora.isoformat()
            supabase_admin.table("assinaturas").insert(dados_assinatura).execute()

        supabase_admin.table("compras_pendentes").update({
            "status": "vinculada",
            "atualizado_em": agora.isoformat(),
        }).eq("compra_id", compra_id).execute()


@app.post("/api/webhooks/cakto")
async def webhook_cakto(request: Request):
    import hashlib
    import hmac
    import json
    import time
    from datetime import datetime, timezone

    segredo = os.getenv("CAKTO_WEBHOOK_SECRET", "").strip()
    if not segredo:
        raise HTTPException(status_code=503, detail="Webhook Cakto ainda não configurado no servidor.")

    corpo = await request.body()
    assinatura_header = request.headers.get("X-Cakto-Signature", "").strip()
    timestamp = request.headers.get("X-Cakto-Timestamp", "").strip()

    try:
        payload = json.loads(corpo.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Payload JSON inválido.")

    autenticado = False
    if assinatura_header and timestamp:
        try:
            timestamp_int = int(timestamp)
        except ValueError:
            raise HTTPException(status_code=401, detail="Timestamp inválido.")
        if abs(time.time() - timestamp_int) > 300:
            raise HTTPException(status_code=401, detail="Webhook expirado.")
        digest = hmac.new(
            segredo.encode("utf-8"),
            timestamp.encode("utf-8") + b"." + corpo,
            hashlib.sha256,
        ).hexdigest()
        assinaturas_recebidas = [parte.strip() for parte in assinatura_header.split(",")]
        autenticado = any(hmac.compare_digest(parte, "v1=" + digest) for parte in assinaturas_recebidas)
    elif isinstance(payload, dict):
        segredo_corpo = str(payload.get("secret") or "")
        autenticado = hmac.compare_digest(segredo_corpo, segredo)

    if not autenticado:
        raise HTTPException(status_code=401, detail="Assinatura do webhook inválida.")

    evento = str(payload.get("event") or "").strip()
    dados = payload.get("data")
    itens = dados if isinstance(dados, list) else [dados]
    processados = 0

    for pedido in itens:
        if not isinstance(pedido, dict):
            continue

        tipo_evento = evento
        pedido_id = str(pedido.get("id") or "").strip()
        assinatura_cakto_id = _mv_id_assinatura_cakto(pedido)
        cliente = pedido.get("customer") or {}
        email = str(cliente.get("email") or "").strip().lower()

        # Só concedemos acesso ao pagamento confirmado do produto principal.
        if tipo_evento in ("purchase_approved", "subscription_renewed", "subscription_late_recovered"):
            if str(pedido.get("status") or "").lower() != "paid":
                continue
            if str(pedido.get("offer_type") or "main").lower() != "main":
                continue
            if not pedido_id or not email or "@" not in email:
                raise HTTPException(status_code=422, detail="Pedido aprovado sem ID ou e-mail válido.")

            plano, produto_slug = _mv_plano_cakto(pedido)
            if not plano:
                raise HTTPException(
                    status_code=422,
                    detail="Oferta Cakto não mapeada. Configure CAKTO_MENSAL_OFFER_ID e CAKTO_ANUAL_OFFER_ID no servidor.",
                )

            existente = (
                supabase_admin.table("compras_pendentes")
                .select("compra_id,email,status,user_id,cakto_subscription_id")
                .eq("compra_id", pedido_id)
                .limit(1)
                .execute()
            )
            linha_existente = existente.data[0] if existente.data else None

            # Se o pedido já foi vinculado, este é um reenvio da mesma compra.
            if linha_existente and linha_existente.get("status") == "vinculada":
                processados += 1
                continue

            user_id = str(linha_existente.get("user_id")) if linha_existente and linha_existente.get("user_id") else None
            if not user_id:
                user_id = _mv_localizar_usuario_email(email)
            if not user_id:
                anterior = (
                    supabase_admin.table("compras_pendentes")
                    .select("user_id")
                    .eq("email", email)
                    .not_.is_("user_id", "null")
                    .order("criado_em", desc=True)
                    .limit(1)
                    .execute()
                )
                if anterior.data and anterior.data[0].get("user_id"):
                    user_id = str(anterior.data[0]["user_id"])

            agora = datetime.now(timezone.utc).isoformat()
            data_minima = {
                "id": pedido_id,
                "status": pedido.get("status"),
                "paidAt": pedido.get("paidAt"),
                "createdAt": pedido.get("createdAt"),
                "offer_type": pedido.get("offer_type"),
                "product": {"id": (pedido.get("product") or {}).get("id")},
                "offer": {"id": (pedido.get("offer") or {}).get("id")},
                "subscription": {"id": assinatura_cakto_id} if assinatura_cakto_id else None,
            }
            atualizacao = {
                "email": email,
                "plano": plano,
                "status": "aprovada",
                "cakto_subscription_id": assinatura_cakto_id,
                "dados_evento": data_minima,
                "produto_slug": produto_slug,
                "atualizado_em": agora,
            }
            if user_id:
                atualizacao["user_id"] = user_id
                atualizacao["vinculada_em"] = agora

            if linha_existente:
                supabase_admin.table("compras_pendentes").update(atualizacao).eq(
                    "compra_id", pedido_id
                ).execute()
            else:
                atualizacao.update({
                    "compra_id": pedido_id,
                    "criado_em": agora,
                    "user_id": user_id,
                    "vinculada_em": agora if user_id else None,
                })
                supabase_admin.table("compras_pendentes").insert(atualizacao).execute()

            if user_id:
                _mv_vincular_compras_e_ativar_assinaturas(email, user_id)
            processados += 1

        elif tipo_evento in ("refund", "chargeback"):
            if not pedido_id:
                continue
            compra_existente = (
                supabase_admin.table("compras_pendentes")
                .select("cakto_subscription_id")
                .eq("compra_id", pedido_id)
                .limit(1)
                .execute()
            )
            if not assinatura_cakto_id and compra_existente.data:
                assinatura_cakto_id = compra_existente.data[0].get("cakto_subscription_id")
            status_compra = "reembolsada" if tipo_evento == "refund" else "chargeback"
            agora = datetime.now(timezone.utc).isoformat()
            supabase_admin.table("compras_pendentes").update({
                "status": status_compra,
                "atualizado_em": agora,
            }).eq("compra_id", pedido_id).execute()

            atualizacao_assinatura = {"status": "cancelada", "atualizado_em": agora}
            if assinatura_cakto_id:
                supabase_admin.table("assinaturas").update(atualizacao_assinatura).eq(
                    "cakto_subscription_id", str(assinatura_cakto_id)
                ).execute()
            else:
                supabase_admin.table("assinaturas").update(atualizacao_assinatura).eq(
                    "compra_id", pedido_id
                ).execute()
            processados += 1

        elif tipo_evento == "subscription_canceled":
            # O cancelamento impede novas cobranças, mas preserva o período já pago.
            # A verificação de vence_em encerra o acesso automaticamente no vencimento.
            processados += 1

        elif tipo_evento in (
            "subscription_late",
            "subscription_renewal_refused",
            "subscription_paused",
            "refund_requested",
            "purchase_refused",
            "pix_gerado",
            "boleto_gerado",
            "pix_expirado",
        ):
            # Não concede acesso nem antecipa o fim de um período já pago.
            processados += 1

    return {"ok": True, "event": evento, "processed": processados}

