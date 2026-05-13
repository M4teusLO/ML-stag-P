# ⚽ ML Media Vault

Cofre local de mídias dos seus anúncios do Mercado Livre. Cole o link do anúncio, ele baixa as fotos e vídeos, organiza num banco PostgreSQL e te dá uma interface web para buscar, filtrar e baixar tudo em ZIP.

Pensado para quem vende camisas de futebol em **múltiplas contas**, **funcionários diferentes**, e precisa ter as mídias salvas independentemente do acesso a cada loja.

---

## O que você consegue fazer

- **Coletar mídias** de qualquer anúncio do ML pelo link
- **Catalogar** com metadados de futebol: time, temporada, modelo (casa/fora/terceira/goleiro/retrô/treino), marca, jogador, número, tamanhos, tags livres
- **Organizar por loja/conta** — cada anúncio é vinculado a uma loja sua
- **Buscar e filtrar** o acervo por qualquer dos campos acima
- **Baixar em ZIP** todas as mídias de um anúncio
- **API JSON** disponível em `/api/listings` para automações futuras

Tudo roda **localmente** em containers — você não depende de nuvem, não tem assinatura, e os arquivos ficam no SEU disco em `./data/media`.

---

## Como subir

**Pré-requisito:** Docker Desktop instalado (Windows/Mac) ou Docker + Docker Compose (Linux).

```bash
# 1. Entrar na pasta do projeto
cd ml-media-vault

# 2. (opcional) copiar o .env de exemplo
cp .env.example .env

# 3. Subir tudo
docker compose up -d --build

# 4. Acessar
# Abra http://localhost:8080 no navegador
```

Para parar:
```bash
docker compose down
```

Para zerar tudo (apaga banco e mídias):
```bash
docker compose down -v
rm -rf data/media
```

---

## Estrutura do projeto

```
ml-media-vault/
├── docker-compose.yml      # orquestra app + banco
├── .env.example            # variáveis de ambiente
├── data/
│   └── media/              # ← suas mídias ficam aqui (no seu disco)
└── backend/
    ├── Dockerfile
    ├── requirements.txt
    └── app/
        ├── main.py         # rotas FastAPI
        ├── database.py     # conexão SQLAlchemy
        ├── models.py       # Store / Listing / Media
        ├── scraper.py      # extrai dados da página do ML
        ├── downloader.py   # baixa imagens/thumbs
        ├── templates/      # HTML (Jinja2)
        └── static/         # CSS
```

---

## Fluxo de uso típico

1. **Cadastre suas lojas** em `/stores` (ex: "FutebolStore Oficial", "RetrôShop", "Camisas Premium")
2. **Adicione anúncios** em `/add` — cole a URL, escolha a loja, preencha time/temporada/modelo
3. **Use o acervo** — em `/` você filtra por time, temporada, modelo ou loja
4. **Baixe em ZIP** quando precisar enviar as mídias pra alguém (designer, vendedor, marketplace novo)

---

## Onde os arquivos ficam

- **Banco de dados:** volume Docker `pgdata` (apenas dentro do container — `docker compose down -v` apaga)
- **Mídias:** pasta `./data/media` na máquina hospedeira, organizada em subpastas por `MLB-ID`

Exemplo:
```
data/media/
├── MLB1234567890/
│   ├── 001_camisa-flamengo-frente.webp
│   ├── 002_camisa-flamengo-costas.webp
│   ├── 003_camisa-flamengo-detalhe.webp
│   └── 004_youtube_abc.jpg
└── MLB9876543210/
    └── ...
```

---

## Endpoints úteis

| Rota | Descrição |
|------|-----------|
| `/` | Acervo com filtros |
| `/add` | Adicionar novo anúncio |
| `/listing/{id}` | Detalhe + galeria + edição |
| `/listing/{id}/download` | Download ZIP de todas as mídias |
| `/stores` | Cadastro de lojas |
| `/api/listings` | Lista de anúncios em JSON |
| `/api/listings/{id}` | Detalhes em JSON |
| `/docs` | Documentação interativa da API (Swagger) |
| `/healthz` | Health check |

---

## Notas técnicas

- O scraper tenta extrair dados em 3 camadas: JSON embutido (`__PRELOADED_STATE__`), JSON-LD, e por último HTML via BeautifulSoup. Isso o torna tolerante a mudanças no layout do ML.
- As URLs de imagem são "promovidas" para 2X de resolução (truque do CDN do ML).
- Vídeos do YouTube embutidos têm a URL salva + thumbnail baixada localmente. Se quiser baixar o vídeo em si, dá pra integrar `yt-dlp` num próximo passo.
- Vídeos nativos do ML (raros) têm a URL salva pra você baixar manualmente, já que o player tem proteções.

## Ideias de próximos passos

- Integrar `yt-dlp` para baixar vídeos do YouTube em MP4
- Importar lote (planilha CSV de URLs)
- Re-scrape automático periódico pra capturar anúncios novos da loja
- Login da API oficial do ML pra pegar anúncios privados/pausados
- Upload manual de mídias extras (estúdio próprio)
