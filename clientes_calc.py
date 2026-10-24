"""Métricas de clientes: frecuencia, estado, tendencia y cartera (saldo a crédito)."""
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func

from models import Venta, VentaItem, ahora, db

ESTADOS = {
    "activo": ("Activo", "ok"),
    "riesgo": ("Se está quedando", "warn"),
    "inactivo": ("Inactivo", "err"),
    "nuevo": ("Sin compras", "gray"),
}


def _pares_por_cliente(desde=None, hasta=None):
    q = (db.session.query(Venta.cliente_id, func.sum(VentaItem.cantidad), func.sum(VentaItem.subtotal))
         .join(VentaItem, VentaItem.venta_id == Venta.id)
         .filter(Venta.anulada.is_(False), Venta.cliente_id.isnot(None)))
    if desde:
        q = q.filter(Venta.fecha >= desde)
    if hasta:
        q = q.filter(Venta.fecha < hasta)
    return {cid: (int(u or 0), v or 0) for cid, u, v in q.group_by(Venta.cliente_id).all()}


def cartera_por_cliente(ids=None):
    """dict cliente_id -> (saldo, vencido, n_facturas_pendientes)."""
    q = Venta.query.filter(Venta.anulada.is_(False), Venta.condicion == "credito",
                           Venta.cliente_id.isnot(None))
    if ids is not None:
        q = q.filter(Venta.cliente_id.in_(ids))
    out = defaultdict(lambda: [0.0, 0.0, 0])
    for v in q.all():
        s = v.saldo
        if s > 0.5:
            o = out[v.cliente_id]
            o[0] += s
            o[2] += 1
            if v.vencida:
                o[1] += s
    return out


def metricas(clientes, hoy):
    """Devuelve dict cliente_id -> métricas. hoy: date."""
    ini_hoy = datetime.combine(hoy, datetime.min.time())
    manana = ini_hoy + timedelta(days=1)
    ult90 = _pares_por_cliente(ini_hoy - timedelta(days=89), manana)
    prev90 = _pares_por_cliente(ini_hoy - timedelta(days=179), ini_hoy - timedelta(days=89))
    total = _pares_por_cliente()
    resumen = {cid: (n, first, last) for cid, n, first, last in (
        db.session.query(Venta.cliente_id, func.count(Venta.id), func.min(Venta.fecha), func.max(Venta.fecha))
        .filter(Venta.anulada.is_(False), Venta.cliente_id.isnot(None))
        .group_by(Venta.cliente_id).all())}
    cart = cartera_por_cliente()

    out = {}
    for c in clientes:
        n, first, last = resumen.get(c.id, (0, None, None))
        frec = ((last - first).days / (n - 1)) if n and n > 1 else None
        dias = (hoy - last.date()).days if last else None
        if not n:
            estado = "nuevo"
        else:
            base = frec or 30
            if dias <= max(30, 1.5 * base):
                estado = "activo"
            elif dias <= max(60, 3 * base):
                estado = "riesgo"
            else:
                estado = "inactivo"
        u90, v90 = ult90.get(c.id, (0, 0))
        up, vp = prev90.get(c.id, (0, 0))
        tendencia = ((v90 - vp) / vp * 100) if vp else (100.0 if v90 else None)
        saldo, vencido, pend = cart.get(c.id, (0.0, 0.0, 0))
        out[c.id] = dict(
            compras=n, primera=first, ultima=last, dias=dias, frecuencia=frec, estado=estado,
            estado_txt=ESTADOS[estado][0], estado_cls=ESTADOS[estado][1],
            u90=u90, v90=v90, tendencia=tendencia,
            total_u=total.get(c.id, (0, 0))[0], total_v=total.get(c.id, (0, 0))[1],
            saldo=saldo, vencido=vencido, pendientes=pend,
            cupo_disp=(c.cupo - saldo) if c.cupo else None,
        )
    return out


def por_mes(cliente_id, hoy, meses=6):
    """Pares comprados por mes (últimos `meses`)."""
    ini = hoy.replace(day=1)
    for _ in range(meses - 1):
        ini = (ini - timedelta(days=1)).replace(day=1)
    datos = defaultdict(int)
    for f, u in (db.session.query(Venta.fecha, VentaItem.cantidad)
                 .join(VentaItem, VentaItem.venta_id == Venta.id)
                 .filter(Venta.cliente_id == cliente_id, Venta.anulada.is_(False),
                         Venta.fecha >= datetime.combine(ini, datetime.min.time())).all()):
        datos[(f.year, f.month)] += u
    salida, k = [], ini
    while k <= hoy:
        salida.append(((k.year, k.month), datos[(k.year, k.month)]))
        k = (k.replace(day=28) + timedelta(days=4)).replace(day=1)
    return salida
