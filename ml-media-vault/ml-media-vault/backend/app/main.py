"""ML Media Vault - aplicação principal."""
import io
import os
import zipfile
import logging
from pathlib import Path
from datetime import datetime
from urllib.parse import urlencode

from fastapi import FastAPI, Request, Depends, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import select, or_, func, desc

from .database import engine, Base, get_db
from .models import Store, Listing, Media
from . import scraper, downloader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

Base.metadata.create_all(bind=engine)

app = FastAPI(title="ML Media Vault", description="Cofre de mídias dos seus anúncios do Mercado Livre")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/media", StaticFiles(directory=os.getenv("MEDIA_DIR", "/data/media")), name="media")


# ----------- helpers -----------

def _get_or_create_store(db: Session, name: str | None, seller_nickname: str | None) -> Store | None:
    if not name and not seller_nickname:
        return None
    name = name or seller_nickname
    store = db.scalar(select(Store).where(Store.name == name))
    if store:
        if seller_nickname and not store.seller_nickname:
            store.seller_nickname = seller_nickname
            db.commit()
        return store
    store = Store(name=name, seller_nickname=seller_nickname)
    db.add(store)
    db.commit()
    db.refresh(store)
    return store


def _download_video_with_fallback(
    v: dict,
    dest: Path,
    idx: int,
    media: Media,
) -> None:
    """
    Tenta baixar o vídeo de verdade via yt-dlp.
    Para YouTube: se falhar, cai para thumbnail.
    Para vídeo nativo: se falhar, registra erro sem thumbnail.
    """
    mtype = v.get("type", "video")
    video_url = v["url"]

    try:
        meta = downloader.download_video(video_url, dest, idx)
        media.local_path = meta["local_path"]
        media.file_size = meta["file_size"]
        media.width = meta["width"]
        media.height = meta["height"]
        media.downloaded_at = datetime.utcnow()
        logger.info("Vídeo baixado OK: %s", media.local_path)

    except Exception as e:
        logger.error("Falha ao baixar vídeo %s — %s", video_url[:80], e)
        media.download_error = str(e)

        # Para YouTube: ao menos salva a thumbnail como fallback
        if mtype == "youtube" and v.get("id"):
            logger.info("Tentando fallback de thumbnail para YouTube %s", v["id"])
            try:
                meta = downloader.download_youtube_thumbnail(v["id"], dest, idx)
                if meta:
                    media.local_path = meta["local_path"]
                    media.file_size = meta["file_size"]
                    media.width = meta["width"]
                    media.height = meta["height"]
                    media.downloaded_at = datetime.utcnow()
                    # Mantém download_error para sinalizar que é só thumb
            except Exception as te:
                logger.error("Fallback de thumbnail também falhou: %s", te)


# ----------- rotas WEB -----------

@app.get("/", response_class=HTMLResponse)
def home(
    request: Request,
    db: Session = Depends(get_db),
    q: str | None = Query(None),
    team: str | None = Query(None),
    season: str | None = Query(None),
    store_id: int | None = Query(None),
    kit: str | None = Query(None),
):
    stmt = select(Listing).order_by(desc(Listing.scraped_at))

    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Listing.title.ilike(like), Listing.tags.ilike(like), Listing.player_name.ilike(like)))
    if team:
        stmt = stmt.where(Listing.team == team)
    if season:
        stmt = stmt.where(Listing.season == season)
    if store_id:
        stmt = stmt.where(Listing.store_id == store_id)
    if kit:
        stmt = stmt.where(Listing.kit_type == kit)

    listings = db.scalars(stmt.limit(200)).all()
    stores = db.scalars(select(Store).order_by(Store.name)).all()

    teams = [t for (t,) in db.execute(select(Listing.team).where(Listing.team.is_not(None)).distinct().order_by(Listing.team)).all()]
    seasons = [s for (s,) in db.execute(select(Listing.season).where(Listing.season.is_not(None)).distinct().order_by(Listing.season)).all()]

    total_listings = db.scalar(select(func.count(Listing.id))) or 0
    total_media = db.scalar(select(func.count(Media.id))) or 0

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "listings": listings,
            "stores": stores,
            "teams": teams,
            "seasons": seasons,
            "q": q or "",
            "filters": {"team": team, "season": season, "store_id": store_id, "kit": kit},
            "totals": {"listings": total_listings, "media": total_media, "stores": len(stores)},
        },
    )


@app.get("/add", response_class=HTMLResponse)
def add_form(request: Request, db: Session = Depends(get_db)):
    stores = db.scalars(select(Store).order_by(Store.name)).all()
    return templates.TemplateResponse(
        "add_listing.html",
        {"request": request, "stores": stores, "error": None, "result": None},
    )


@app.post("/add", response_class=HTMLResponse)
def add_submit(
    request: Request,
    url: str = Form(...),
    store_name: str | None = Form(None),
    team: str | None = Form(None),
    season: str | None = Form(None),
    kit_type: str | None = Form(None),
    brand: str | None = Form(None),
    player_name: str | None = Form(None),
    player_number: str | None = Form(None),
    sizes: str | None = Form(None),
    tags: str | None = Form(None),
    db: Session = Depends(get_db),
):
    url = url.strip()
    if not url:
        raise HTTPException(400, "URL é obrigatória")

    existing = db.scalar(select(Listing).where(Listing.url == url))
    if existing:
        return RedirectResponse(f"/listing/{existing.id}", status_code=303)

    try:
        data = scraper.scrape(url)
    except Exception as e:
        logger.exception("Erro no scrape")
        stores = db.scalars(select(Store).order_by(Store.name)).all()
        return templates.TemplateResponse(
            "add_listing.html",
            {"request": request, "stores": stores, "error": f"Erro ao coletar a página: {e}", "result": None},
            status_code=400,
        )

    store = _get_or_create_store(db, store_name, data.get("seller_nickname"))

    listing = Listing(
        ml_id=data.get("ml_id"),
        url=url,
        title=data.get("title"),
        price=data.get("price"),
        description=data.get("description"),
        team=team or None,
        season=season or None,
        kit_type=kit_type or None,
        brand=brand or None,
        player_name=player_name or None,
        player_number=player_number or None,
        sizes=sizes or None,
        tags=tags or None,
        store_id=store.id if store else None,
        raw_data=data.get("raw"),
        scraped_at=datetime.utcnow(),
    )
    db.add(listing)
    db.commit()
    db.refresh(listing)

    dest = downloader.listing_dir(listing.id, listing.ml_id)
    pics = data.get("pictures", [])
    vids = data.get("videos", [])

    # ── Imagens ───────────────────────────────────────────────────────────────
    for idx, pic_url in enumerate(pics, start=1):
        if downloader.is_placeholder(pic_url):
            logger.info("Placeholder ignorado na posição %d: %s…", idx, pic_url[:60])
            continue

        media = Media(
            listing_id=listing.id, type="image", source_url=pic_url, position=idx,
        )
        try:
            meta = downloader.download_image(pic_url, dest, idx)
            media.local_path = meta["local_path"]
            media.file_size = meta["file_size"]
            media.width = meta["width"]
            media.height = meta["height"]
            media.downloaded_at = datetime.utcnow()
        except Exception as e:
            logger.error("Falha ao baixar imagem %s — %s", pic_url[:80], e)
            media.download_error = str(e)
        db.add(media)

    # ── Vídeos ────────────────────────────────────────────────────────────────
    for idx, v in enumerate(vids, start=len(pics) + 1):
        mtype = v.get("type", "video")
        media = Media(
            listing_id=listing.id,
            type=mtype if mtype == "youtube" else "video",
            source_url=v["url"],
            position=idx,
        )
        _download_video_with_fallback(v, dest, idx, media)
        db.add(media)

    db.commit()
    return RedirectResponse(f"/listing/{listing.id}", status_code=303)


@app.get("/listing/{listing_id}", response_class=HTMLResponse)
def listing_detail(listing_id: int, request: Request, db: Session = Depends(get_db)):
    listing = db.get(Listing, listing_id)
    if not listing:
        raise HTTPException(404)
    stores = db.scalars(select(Store).order_by(Store.name)).all()
    return templates.TemplateResponse(
        "listing_detail.html",
        {"request": request, "listing": listing, "stores": stores},
    )


@app.post("/listing/{listing_id}/update", response_class=HTMLResponse)
def listing_update(
    listing_id: int,
    team: str | None = Form(None),
    season: str | None = Form(None),
    kit_type: str | None = Form(None),
    brand: str | None = Form(None),
    player_name: str | None = Form(None),
    player_number: str | None = Form(None),
    sizes: str | None = Form(None),
    tags: str | None = Form(None),
    store_id: str | None = Form(None),
    db: Session = Depends(get_db),
):
    listing = db.get(Listing, listing_id)
    if not listing:
        raise HTTPException(404)
    listing.team = team or None
    listing.season = season or None
    listing.kit_type = kit_type or None
    listing.brand = brand or None
    listing.player_name = player_name or None
    listing.player_number = player_number or None
    listing.sizes = sizes or None
    listing.tags = tags or None
    listing.store_id = int(store_id) if store_id else None
    db.commit()
    return RedirectResponse(f"/listing/{listing_id}", status_code=303)


@app.post("/listing/{listing_id}/delete")
def listing_delete(listing_id: int, db: Session = Depends(get_db)):
    listing = db.get(Listing, listing_id)
    if not listing:
        raise HTTPException(404)
    db.delete(listing)
    db.commit()
    return RedirectResponse("/", status_code=303)


@app.get("/listing/{listing_id}/download")
def listing_download_zip(listing_id: int, db: Session = Depends(get_db)):
    """Baixa todas as mídias do anúncio em um ZIP."""
    listing = db.get(Listing, listing_id)
    if not listing:
        raise HTTPException(404)

    media_root = Path(os.getenv("MEDIA_DIR", "/data/media"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for m in listing.media:
            if not m.local_path:
                continue
            file_path = media_root / m.local_path
            if file_path.exists():
                zf.write(file_path, arcname=Path(m.local_path).name)

    buf.seek(0)
    fname = f"{listing.ml_id or 'listing'}_{listing.id}.zip"
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/stores", response_class=HTMLResponse)
def stores_page(request: Request, db: Session = Depends(get_db)):
    stores = db.scalars(select(Store).order_by(Store.name)).all()
    counts = dict(db.execute(select(Listing.store_id, func.count(Listing.id)).group_by(Listing.store_id)).all())
    return templates.TemplateResponse(
        "stores.html",
        {"request": request, "stores": stores, "counts": counts},
    )


@app.post("/stores/add")
def stores_add(name: str = Form(...), seller_nickname: str | None = Form(None), notes: str | None = Form(None), db: Session = Depends(get_db)):
    if not name.strip():
        raise HTTPException(400, "Nome é obrigatório")
    existing = db.scalar(select(Store).where(Store.name == name.strip()))
    if not existing:
        db.add(Store(name=name.strip(), seller_nickname=seller_nickname, notes=notes))
        db.commit()
    return RedirectResponse("/stores", status_code=303)


# ----------- API JSON -----------

@app.get("/api/listings")
def api_listings(db: Session = Depends(get_db)):
    rows = db.scalars(select(Listing).order_by(desc(Listing.scraped_at))).all()
    out = []
    for r in rows:
        out.append({
            "id": r.id, "ml_id": r.ml_id, "url": r.url, "title": r.title,
            "team": r.team, "season": r.season, "kit_type": r.kit_type,
            "brand": r.brand, "store_id": r.store_id, "price": r.price,
            "media_count": len(r.media),
        })
    return out


@app.get("/api/listings/{listing_id}")
def api_listing(listing_id: int, db: Session = Depends(get_db)):
    r = db.get(Listing, listing_id)
    if not r:
        raise HTTPException(404)
    return {
        "id": r.id, "ml_id": r.ml_id, "url": r.url, "title": r.title,
        "team": r.team, "season": r.season, "kit_type": r.kit_type,
        "brand": r.brand, "store_id": r.store_id, "price": r.price,
        "tags": r.tags, "player_name": r.player_name, "player_number": r.player_number,
        "sizes": r.sizes,
        "media": [
            {"type": m.type, "source_url": m.source_url, "local_path": m.local_path,
             "width": m.width, "height": m.height, "position": m.position}
            for m in r.media
        ],
    }


@app.get("/healthz")
def healthz():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}