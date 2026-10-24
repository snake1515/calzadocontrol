"""Depreciación total de los equipos en un rango de fechas."""
from datetime import timedelta

from models import Activo


def activos_vigentes():
    return Activo.query.filter_by(eliminado=False).all()


def depreciacion_periodo(desde, hasta, activos=None):
    """desde/hasta: date; hasta es inclusivo."""
    activos = activos if activos is not None else activos_vigentes()
    fin = hasta + timedelta(days=1)
    return sum(a.depreciacion_entre(desde, fin) for a in activos)


def depreciacion_dia(dia, activos):
    return sum(a.depreciacion_entre(dia, dia + timedelta(days=1)) for a in activos)
