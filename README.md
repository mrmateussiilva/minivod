# MiniVOD

Servidor VOD mínimo em Python, SQLite e FFmpeg, com scanner incremental, HLS sob demanda, cache local e API Xtream básica.

## Deploy com Docker Compose

O Compose cria somente dois containers: `minivod` (HTTP/HLS/Xtream) e `scanner` (indexação incremental). Dados, mídia e cache são bind mounts do host e não são removidos por `docker compose down` nem por rebuilds.

Requisitos: Docker com Docker Compose e os diretórios abaixo no host:

```text
/home/server/downloads/
├── iptv/
└── vod.db

/home/server/vod-cache/
```

Configure e suba:

```bash
cp .env.example .env
nano .env

docker compose build
docker compose up -d
```

Status e logs:

```bash
docker compose ps
docker compose logs -f minivod
docker compose logs -f scanner
```

Teste local:

```bash
curl http://localhost:8079/health
```

Parar os containers sem apagar mídia, SQLite ou cache:

```bash
docker compose down
```

Atualizar a aplicação:

```bash
git pull
docker compose up -d --build
```

## Configuração

`.env` define os bind mounts, a porta publicada, URL base retornada pela API e o intervalo do scanner:

```env
MINIVOD_DATA=/home/server/downloads
MINIVOD_CACHE=/home/server/vod-cache
MINIVOD_PORT=8079
MINIVOD_BASE_URL=http://192.168.15.7:8079
SCAN_INTERVAL=300
```

Dentro dos containers, `MINIVOD_DATA` é montado em `/data` (`/data/iptv` e `/data/vod.db`) e `MINIVOD_CACHE` em `/cache`. O servidor escuta em `0.0.0.0:8079`; a porta publicada no host é controlada por `MINIVOD_PORT`.

## Usuários Xtream

Criar usuário:

```bash
docker compose exec minivod python3 /app/hls_server.py --db /data/vod.db --create-user cliente01
```

Listar usuários:

```bash
docker compose exec minivod python3 /app/hls_server.py --db /data/vod.db --list-users
```

Habilitar ou desabilitar:

```bash
docker compose exec minivod python3 /app/hls_server.py --db /data/vod.db --enable-user cliente01
docker compose exec minivod python3 /app/hls_server.py --db /data/vod.db --disable-user cliente01
```

## Cloudflare Tunnel

Não há `cloudflared` no Compose. Depois de confirmar que o MiniVOD responde em `http://IP_DO_SERVIDOR:8079`, configure no dashboard do Cloudflare Tunnel um **Public Hostname** para `vod.meudominio.com` com serviço HTTP apontando para:

```text
http://localhost:8079
```

Depois atualize:

```env
MINIVOD_BASE_URL=https://vod.meudominio.com
```

E reinicie os serviços:

```bash
docker compose up -d
```
