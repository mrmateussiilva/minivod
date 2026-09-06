# MiniVOD

Servidor VOD mínimo em Python para biblioteca local, com:

- scanner incremental para SQLite;
- metadados via `ffprobe`;
- HLS sob demanda;
- segmentos MPEG-TS (`.ts`);
- cache local;
- API VOD simples;
- compatibilidade Xtream básica;
- usuários locais;
- deploy via systemd;
- pronto para ficar atrás de Cloudflare Tunnel.

## Estrutura

```text
app/
  hls_server.py
  scan_vod.py
config/
  minivod.env.example
cloudflared/
  config.yml.example
systemd/
  minivod.service
  minivod-scan.service
  minivod-scan.timer
scripts/
  install.sh
  run-server.sh
  scan.sh
  user.sh
```

## Desenvolvimento / teste local

O projeto usa apenas Python 3 padrão, SQLite e FFmpeg.

```bash
sudo apt install python3 ffmpeg sqlite3
```

O banco `vod.db`, mídia e cache **não entram no Git**.

## Instalação no Debian

Clone o repositório e rode:

```bash
sudo ./scripts/install.sh
```

Edite:

```bash
sudo nano /etc/minivod/minivod.env
```

Exemplo:

```env
MINIVOD_DB=/home/server/downloads/vod.db
MINIVOD_MEDIA=/home/server/downloads/iptv
MINIVOD_CACHE=/home/server/vod-cache
MINIVOD_HOST=127.0.0.1
MINIVOD_PORT=8079
MINIVOD_BASE_URL=https://vod.seudominio.com
MINIVOD_SEGMENT_TIME=6
MINIVOD_PLAYLIST_WAIT=30
MINIVOD_COPY_ONLY=0
```

Suba o servidor:

```bash
sudo systemctl enable --now minivod
sudo systemctl enable --now minivod-scan.timer
```

Logs:

```bash
journalctl -u minivod -f
```

Status:

```bash
curl http://127.0.0.1:8079/health
```

## Usuários Xtream

Criar:

```bash
sudo /opt/minivod/scripts/user.sh create cliente01
```

Listar:

```bash
sudo /opt/minivod/scripts/user.sh list
```

Desabilitar:

```bash
sudo /opt/minivod/scripts/user.sh disable cliente01
```

## Cloudflare Tunnel

O MiniVOD deve continuar ouvindo apenas em:

```text
127.0.0.1:8079
```

Use `cloudflared/config.yml.example` como referência. O hostname público deve apontar para:

```text
http://127.0.0.1:8079
```

E `MINIVOD_BASE_URL` deve ser o domínio HTTPS público, por exemplo:

```env
MINIVOD_BASE_URL=https://vod.seudominio.com
```

## Xtream

No player:

```text
Server:   https://vod.seudominio.com
Username: cliente01
Password: ********
```

Rotas principais:

```text
/player_api.php
/get.php
/movie/{username}/{password}/{id}.m3u8
```

## Atualização

Após `git pull`:

```bash
sudo ./scripts/install.sh
sudo systemctl restart minivod
```
