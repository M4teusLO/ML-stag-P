from datetime import datetime
from sqlalchemy import (
    String, Integer, Float, DateTime, ForeignKey, Text, UniqueConstraint, Index,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base


class Store(Base):
    """Cada loja/conta do Mercado Livre - serve para você saber de onde veio cada anúncio."""
    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    seller_nickname: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    listings: Mapped[list["Listing"]] = relationship(
        "Listing", back_populates="store", cascade="all, delete-orphan"
    )


class Listing(Base):
    """Anúncio do ML, com metadados de futebol."""
    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(primary_key=True)
    ml_id: Mapped[str | None] = mapped_column(String(40), index=True)
    url: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    title: Mapped[str | None] = mapped_column(String(500))
    price: Mapped[float | None] = mapped_column(Float)
    description: Mapped[str | None] = mapped_column(Text)

    # Metadados específicos de futebol
    team: Mapped[str | None] = mapped_column(String(120), index=True)
    season: Mapped[str | None] = mapped_column(String(40), index=True)
    kit_type: Mapped[str | None] = mapped_column(String(40), index=True)  # home/away/third/gk/retro
    brand: Mapped[str | None] = mapped_column(String(80), index=True)
    player_name: Mapped[str | None] = mapped_column(String(120))
    player_number: Mapped[str | None] = mapped_column(String(10))
    sizes: Mapped[str | None] = mapped_column(String(200))  # csv: "P,M,G,GG"
    tags: Mapped[str | None] = mapped_column(String(500))  # csv livre

    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"))

    raw_data: Mapped[dict | None] = mapped_column(JSONB)
    scraped_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    store: Mapped["Store"] = relationship("Store", back_populates="listings")
    media: Mapped[list["Media"]] = relationship(
        "Media", back_populates="listing", cascade="all, delete-orphan",
        order_by="Media.position",
    )

    __table_args__ = (
        Index("ix_listings_team_season", "team", "season"),
    )


class Media(Base):
    """Cada foto ou vídeo associado a um anúncio."""
    __tablename__ = "media"

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(20))   # 'image' | 'video' | 'youtube'
    source_url: Mapped[str] = mapped_column(String(800))
    local_path: Mapped[str | None] = mapped_column(String(500))  # caminho relativo dentro de /data/media
    file_size: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer, default=0)
    downloaded_at: Mapped[datetime | None] = mapped_column(DateTime)
    download_error: Mapped[str | None] = mapped_column(Text)

    listing: Mapped["Listing"] = relationship("Listing", back_populates="media")

    __table_args__ = (
        UniqueConstraint("listing_id", "source_url", name="uq_media_listing_source"),
    )
