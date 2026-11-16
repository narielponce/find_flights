from datetime import date, datetime, timezone
from enum import Enum
import re
from typing import Any, Optional
from pydantic import BaseModel, Field as PydanticField, field_validator
from sqlmodel import Field, SQLModel


class TripType(str, Enum):
    FLIGHT = "flight"
    PACKAGE = "package"


COMMON_IATA_MAP = {
    "CORDOBA": "COR",
    "CÓRDOBA": "COR",
    "BUENOS AIRES": "BUE",
    "EZEIZA": "EZE",
    "AEROPARQUE": "AEP",
    "MENDOZA": "MDZ",
    "ROSARIO": "ROS",
    "BARILOCHE": "BRC",
    "SALTA": "SLA",
    "IGUAZU": "IGR",
    "IGUAZÚ": "IGR",
    "RIO DE JANEIRO": "GIG",
    "RÍO DE JANEIRO": "GIG",
    "RIO": "GIG",
    "RÍO": "GIG",
    "SAO PAULO": "GRU",
    "SÃO PAULO": "GRU",
    "SAN PABLO": "GRU",
    "FLORIANOPOLIS": "FLN",
    "FLORIANÓPOLIS": "FLN",
    "SANTIAGO": "SCL",
    "LIMA": "LIM",
    "BOGOTA": "BOG",
    "BOGOTÁ": "BOG",
    "CANCUN": "CUN",
    "CANCÚN": "CUN",
    "MIAMI": "MIA",
    "MADRID": "MAD",
    "BARCELONA": "BCN",
    "ROMA": "FCO",
}


def normalize_iata_code(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if raw in COMMON_IATA_MAP:
        return COMMON_IATA_MAP[raw]
    # Si viene como "COR (Córdoba)" o "Córdoba (COR)", extraer código de 3 letras entre paréntesis o al inicio
    paren_match = re.search(r"\(([A-Z]{3})\)", raw)
    if paren_match:
        return paren_match.group(1)
    token_match = re.match(r"^([A-Z]{3})\b", raw)
    if token_match and len(raw) == 3:
        return token_match.group(1)
    for city, code in COMMON_IATA_MAP.items():
        if city in raw:
            return code
    # Fallback: primeros 3 caracteres alfabéticos
    letters = "".join(ch for ch in raw if ch.isalpha())
    return (letters[:3] or "UNK").upper()


# ==========================================
# Modelos de Base de Datos (SQLModel / PostgreSQL)
# ==========================================

class AlertSubscription(SQLModel, table=True):
    __tablename__ = "alert_subscriptions"

    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: str = Field(index=True, max_length=64, description="Telegram Chat ID")
    origin: str = Field(max_length=3, index=True, description="Código IATA origen (ej. COR)")
    destination: str = Field(max_length=3, index=True, description="Código IATA destino (ej. GIG)")
    date_from: date = Field(description="Fecha de ida")
    date_to: date = Field(description="Fecha de regreso")
    max_price: float = Field(gt=0, description="Presupuesto máximo en USD")
    max_stops: int = Field(default=1, ge=0, le=3, description="Máximo de escalas permitidas")
    trip_type: TripType = Field(default=TripType.FLIGHT, description="flight o package (vuelo + hotel)")
    is_active: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PriceHistory(SQLModel, table=True):
    __tablename__ = "price_history"

    id: Optional[int] = Field(default=None, primary_key=True)
    origin: str = Field(max_length=3, index=True)
    destination: str = Field(max_length=3, index=True)
    trip_type: TripType = Field(index=True)
    departure_date: date
    return_date: date
    provider: str = Field(max_length=32)
    airline_or_hotel: str = Field(max_length=128)
    total_price: float = Field(gt=0)
    currency: str = Field(default="USD", max_length=3)
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)


# ==========================================
# Esquemas DTO / Pydantic (Requests & Responses)
# ==========================================

class AlertCreateRequest(BaseModel):
    chat_id: str = PydanticField(..., examples=["123456789"])
    origin: str = PydanticField(..., examples=["COR"])
    destination: str = PydanticField(..., examples=["GIG"])
    date_from: date = PydanticField(..., examples=["2026-03-10"])
    date_to: date = PydanticField(..., examples=["2026-03-20"])
    max_price: float = PydanticField(..., gt=0, examples=[800.0])
    max_stops: int = PydanticField(default=1, ge=0, le=5, examples=[1])
    trip_type: TripType = PydanticField(default=TripType.FLIGHT, examples=["flight"])

    @field_validator("chat_id", mode="before")
    @classmethod
    def coerce_chat_id(cls, v: Any) -> str:
        return str(v).strip()

    @field_validator("origin", "destination", mode="before")
    @classmethod
    def coerce_iata(cls, v: Any) -> str:
        return normalize_iata_code(v)

    @field_validator("max_price", mode="before")
    @classmethod
    def coerce_max_price(cls, v: Any) -> float:
        if isinstance(v, str):
            cleaned = re.sub(r"[^\d.]", "", v)
            return float(cleaned) if cleaned else 0.0
        return float(v)

    @field_validator("max_stops", mode="before")
    @classmethod
    def coerce_max_stops(cls, v: Any) -> int:
        if v is None or v == "" or v == "undefined":
            return 1
        if isinstance(v, str):
            cleaned = re.sub(r"[^\d]", "", v)
            return int(cleaned) if cleaned else 1
        return int(v)

    @field_validator("trip_type", mode="before")
    @classmethod
    def coerce_trip_type(cls, v: Any) -> TripType:
        if not v or str(v).strip().lower() in ("flight", "vuelo", "flights", "undefined"):
            return TripType.FLIGHT
        if str(v).strip().lower() in ("package", "paquete", "hotel", "both"):
            return TripType.PACKAGE
        return TripType.FLIGHT


class SearchExecuteRequest(BaseModel):
    alert_id: Optional[int] = PydanticField(
        default=None,
        description="Si se omite o es null, ejecuta todas las alertas activas."
    )


class OfferCandidate(BaseModel):
    """Estructura interna normalizada entre proveedores (Amadeus / SerpApi)."""
    provider: str
    origin: str
    destination: str
    departure_date: date
    return_date: date
    trip_type: TripType
    airline: str
    stops: int
    max_layover_minutes: int
    total_duration_minutes: int
    flight_price: float
    hotel_name: Optional[str] = None
    hotel_price: float = 0.0
    taxes_and_fees: float = 0.0
    currency: str = "USD"
    reference_link: str

    @property
    def estimated_real_price(self) -> float:
        return round(self.flight_price + self.hotel_price + self.taxes_and_fees, 2)


class DealSummary(BaseModel):
    """JSON ultra-liviano de cada oferta en el Top 3."""
    rank: int
    summary: str
    real_price_usd: float
    historical_avg_usd: float
    savings_pct: float
    stops: int
    max_layover_hours: float
    reference_link: str


class AlertExecutionResult(BaseModel):
    alert_id: int
    chat_id: str
    route: str
    trip_type: TripType
    target_max_price: float
    top_deals: list[DealSummary]


class SearchExecuteResponse(BaseModel):
    executed_alerts: int
    notifications: list[AlertExecutionResult]
