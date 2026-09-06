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
MINIVOD_ADMIN_USER=admin
MINIVOD_ADMIN_PASSWORD=uma-senha-forte
SCAN_INTERVAL=300
```

Dentro dos containers, `MINIVOD_DATA` é montado em `/data` (`/data/iptv` e `/data/vod.db`) e `MINIVOD_CACHE` em `/cache`. O servidor escuta em `0.0.0.0:8079`; a porta publicada no host é controlada por `MINIVOD_PORT`.

## Painel administrativo

Com as duas variáveis abaixo preenchidas, o painel fica disponível em `https://vod.seudominio.com/admin` ou em `http://IP_DO_SERVIDOR:8079/admin`. O navegador solicitará autenticação HTTP Basic.

```env
MINIVOD_ADMIN_USER=admin
MINIVOD_ADMIN_PASSWORD=uma-senha-forte
```

Depois de alterar `.env`, aplique a configuração:

```bash
docker compose up -d
```

Se `MINIVOD_ADMIN_PASSWORD` ficar vazia, o painel permanece desabilitado.

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

## Compatibilidade IPTV

O MiniVOD oferece VOD por API compatível com Xtream:

```text
https://vod.atrasado.online/player_api.php
https://vod.atrasado.online/get.php
https://vod.atrasado.online/xmltv.php
```

No player Xtream:

```text
Server:   https://vod.atrasado.online
Username: cliente01
Password: ********
```

O catálogo pode ser consumido de três formas, sem duplicar mídia ou dados:

- **VOD Xtream:** cada coleção é uma categoria; `get_vod_streams` sem
  `category_id` preserva o catálogo completo e, com `category_id`, retorna
  apenas a coleção solicitada.
- **Series Xtream:** a categoria única `Coleções` contém uma série por
  coleção; seus vídeos aparecem como episódios da `Season 1`. Use
  `get_series_categories`, `get_series` e
  `get_series_info&series_id={collection_id}`.
- **M3U Plus:** `/get.php?type=m3u_plus&output=m3u8` agrupa entradas por
  `group-title` da coleção. `type=m3u` continua disponível na forma simples.

As rotas de reprodução HLS aceitam tanto
`/movie/{username}/{password}/{id}.m3u8` como
`/series/{username}/{password}/{id}.m3u8`; ambas usam o mesmo cache e
processo FFmpeg. Também são aceitos os aliases `/player_api`, `/get` e
`/xmltv`.

Live e EPG real não são implementados: as ações Live retornam listas vazias e
o XMLTV é um documento vazio válido. O MiniVOD entrega HLS (`m3u8`/`hls`);
`output=ts` e `output=mpegts` mantêm a playlist HLS legada, pois não há
stream MPEG-TS contínuo nesta implementação.

## Capas de coleções

O scanner usa imagens já presentes em cada pasta de coleção. Ele prioriza
`cover`, `poster`, `folder` e `capa` (JPG, JPEG, PNG ou WebP), depois imagens
na raiz, em diretórios de fotos conhecidos e, por fim, qualquer imagem válida.
Arquivos com nomes como `thumb`, `watermark`, `preview` ou `logo` só são usados
se não houver alternativa. A capa selecionada fica estável enquanto o arquivo
existir; não há geração, download ou cópia para o cache.

As capas são expostas em `/covers/{collection_id}` e recebem cache HTTP de um
dia. Elas aparecem como `cover` no Series, `stream_icon` no VOD e `tvg-logo`
no M3U Plus quando disponíveis. O painel `/admin` permite visualizar os
candidatos existentes, selecionar uma imagem segura dentro da coleção ou
executar novamente a seleção automática.

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
