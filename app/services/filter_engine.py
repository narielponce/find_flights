from sqlalchemy import func
from sqlmodel import Session, select

from app.config import settings
from app.models import (
    AlertSubscription,
    DealSummary,
    OfferCandidate,
    PriceHistory,
)


class DeterministicFilterEngine:
    """
    Motor de filtrado determinístico para vuelos y paquetes:
    - Filtra escalas > alert.max_stops
    - Filtra tiempos de conexión en aeropuerto > 6 horas (360 min por defecto)
    - Filtra precios reales superiores al presupuesto máximo del usuario
    - Compara contra el promedio histórico en `price_history` y persiste nuevas muestras
    - Devuelve únicamente el Top 3 de ofertas en formato ultra-compacto para el LLM de n8n
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def _get_historical_average(self, alert: AlertSubscription) -> float:
        stmt = select(
            func.count(PriceHistory.id),
            func.avg(PriceHistory.total_price),
        ).where(
            PriceHistory.origin == alert.origin.upper(),
            PriceHistory.destination == alert.destination.upper(),
            PriceHistory.trip_type == alert.trip_type,
        )
        row = self.session.exec(stmt).one_or_none()
        if not row or row[1] is None:
            # Si aún no hay historial para el tramo, usamos el max_price como línea base inicial
            return float(alert.max_price)
        count_samples, avg_price = int(row[0] or 0), float(row[1])
        # En fase de arranque (< 10 muestras históricas), tomamos el mayor entre el promedio y max_price
        if count_samples < 10:
            return round(max(avg_price, float(alert.max_price)), 2)
        return round(avg_price, 2)

    def _record_price_history(self, candidates: list[OfferCandidate]) -> None:
        for c in candidates:
            label = c.airline if not c.hotel_name else f"{c.airline} + {c.hotel_name}"
            entry = PriceHistory(
                origin=c.origin.upper(),
                destination=c.destination.upper(),
                trip_type=c.trip_type,
                departure_date=c.departure_date,
                return_date=c.return_date,
                provider=c.provider,
                airline_or_hotel=label[:128],
                total_price=c.estimated_real_price,
                currency=c.currency,
            )
            self.session.add(entry)
        self.session.commit()

    def evaluate_and_rank(
        self, alert: AlertSubscription, candidates: list[OfferCandidate]
    ) -> list[DealSummary]:
        if not candidates:
            return []

        historical_avg = self._get_historical_average(alert)

        # 1. Filtrado determinístico estricto
        valid_candidates: list[OfferCandidate] = []
        for offer in candidates:
            if offer.stops > alert.max_stops:
                continue
            if offer.max_layover_minutes > settings.max_layover_minutes:
                continue
            if offer.estimated_real_price > alert.max_price:
                continue
            valid_candidates.append(offer)

        # Persistimos los candidatos válidos (no absurdos) para enriquecer la serie histórica
        if valid_candidates:
            self._record_price_history(valid_candidates)

        # 2. Deduplicación básica y ordenamiento por precio real estimado ascendente
        seen_keys: set[tuple[str, float, int]] = set()
        unique_candidates: list[OfferCandidate] = []
        for offer in sorted(valid_candidates, key=lambda x: x.estimated_real_price):
            dedup_key = (offer.airline, offer.estimated_real_price, offer.stops)
            if dedup_key not in seen_keys:
                seen_keys.add(dedup_key)
                unique_candidates.append(offer)

        # 3. Selección del Top 3 y construcción del payload ultra-liviano
        top_deals: list[DealSummary] = []
        for idx, offer in enumerate(unique_candidates[:3], start=1):
            real_price = offer.estimated_real_price
            savings_pct = (
                round(((historical_avg - real_price) / historical_avg) * 100, 1)
                if historical_avg > 0
                else 0.0
            )
            summary_parts = [
                f"{offer.origin}->{offer.destination}",
                f"({offer.departure_date.isoformat()} a {offer.return_date.isoformat()})",
                f"via {offer.airline}",
            ]
            if offer.hotel_name:
                summary_parts.append(f"+ {offer.hotel_name}")

            top_deals.append(
                DealSummary(
                    rank=idx,
                    summary=" ".join(summary_parts),
                    real_price_usd=real_price,
                    historical_avg_usd=historical_avg,
                    savings_pct=savings_pct,
                    stops=offer.stops,
                    max_layover_hours=round(offer.max_layover_minutes / 60.0, 1),
                    reference_link=offer.reference_link,
                )
            )

        return top_deals

