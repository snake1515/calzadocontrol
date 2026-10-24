"""Liquidación de nómina: salario fijo, destajo por procesos, aportes y prestaciones.

Los porcentajes son los de ley en Colombia (2026) y se pueden apagar desde Configuración
para talleres que pagan solo destajo. Es una ayuda para controlar costos, no reemplaza
la liquidación de un contador ni la planilla PILA.
"""
from datetime import datetime, timedelta

from models import PagoNomina, Produccion, ProduccionMO, ahora, db
from services import ErrorNegocio

SALUD_EMPLEADO = 4.0
PENSION_EMPLEADO = 4.0
PENSION_EMPLEADOR = 12.0
CAJA = 4.0
SALUD_EMPLEADOR = 8.5
CESANTIAS = 8.33
INTERESES_CES = 1.0
PRIMA = 8.33
VACACIONES = 4.17


def destajo_pendiente(empleado, desde, hasta):
    """Líneas de mano de obra del empleado en lotes del periodo que aún no se han pagado."""
    ini = datetime.combine(desde, datetime.min.time())
    fin = datetime.combine(hasta + timedelta(days=1), datetime.min.time())
    return (ProduccionMO.query.join(Produccion, ProduccionMO.produccion_id == Produccion.id)
            .filter(ProduccionMO.empleado_id == empleado.id, ProduccionMO.nomina_id.is_(None),
                    Produccion.anulada.is_(False), Produccion.fecha >= ini, Produccion.fecha < fin)
            .order_by(Produccion.fecha).all())


def calcular(empleado, cfg, dias, destajo, horas_extra=0.0, bonificaciones=0.0, otras_deducciones=0.0,
             seguridad_social=True, prestaciones=True):
    """Devuelve un dict con todos los valores de la liquidación."""
    dias = max(min(int(dias or 0), 31), 0)
    basico = (empleado.salario or 0) * dias / 30 if empleado.tipo_pago in ("fijo", "mixto") else 0.0
    if empleado.tipo_pago == "fijo":
        destajo = 0.0
    ibc = basico + destajo + horas_extra
    mensual = ibc * 30 / dias if dias else 0
    aux = 0.0
    if empleado.aux_transporte and dias and mensual <= 2 * (cfg.smmlv or 0):
        aux = (cfg.aux_transporte or 0) * dias / 30
    salud = pension = aportes = 0.0
    if seguridad_social:
        salud = ibc * SALUD_EMPLEADO / 100
        pension = ibc * PENSION_EMPLEADO / 100
        pct = PENSION_EMPLEADOR + (cfg.nom_arl_pct or 0) + CAJA
        if cfg.nom_salud_empleador:
            pct += SALUD_EMPLEADOR
        aportes = ibc * pct / 100
    prov = 0.0
    if prestaciones:
        prov = (ibc + aux) * (CESANTIAS + INTERESES_CES + PRIMA) / 100 + (basico + destajo) * VACACIONES / 100
    devengado = basico + aux + destajo + horas_extra + bonificaciones
    deducciones = salud + pension + otras_deducciones
    costo = devengado + aportes + prov
    r = dict(dias=dias, basico=basico, aux_transporte=aux, destajo=destajo, horas_extra=horas_extra,
             bonificaciones=bonificaciones, deduccion_salud=salud, deduccion_pension=pension,
             otras_deducciones=otras_deducciones, devengado=devengado, deducciones=deducciones,
             neto=devengado - deducciones, aportes=aportes, provisiones=prov, costo_empresa=costo,
             gasto=costo - destajo, ibc=ibc)
    return {k: (round(v) if isinstance(v, float) else v) for k, v in r.items()}


def registrar(empleado, cfg, desde, hasta, fecha_pago, dias, horas_extra=0.0, bonificaciones=0.0,
              otras_deducciones=0.0, deducciones_detalle="", seguridad_social=True, prestaciones=True,
              metodo="Efectivo", nota="", usuario_id=None):
    errores = []
    if not empleado:
        errores.append("Elige el empleado.")
    if not desde or not hasta or hasta < desde:
        errores.append("Revisa el periodo: la fecha final debe ser igual o posterior a la inicial.")
    if min(horas_extra, bonificaciones, otras_deducciones) < 0:
        errores.append("Los valores no pueden ser negativos.")
    if otras_deducciones > 0 and not (deducciones_detalle or "").strip():
        errores.append("Escribe el concepto de las otras deducciones (anticipo, préstamo…).")
    if errores:
        raise ErrorNegocio(errores)
    lineas = destajo_pendiente(empleado, desde, hasta) if empleado.tipo_pago != "fijo" else []
    destajo = sum(l.total for l in lineas)
    c = calcular(empleado, cfg, dias, destajo, horas_extra, bonificaciones, otras_deducciones,
                 seguridad_social, prestaciones)
    if c["devengado"] <= 0:
        raise ErrorNegocio("No hay nada que pagar en ese periodo: sin salario ni destajo pendiente.")
    if c["neto"] < 0:
        raise ErrorNegocio("Las deducciones superan lo devengado.")
    pago = PagoNomina(empleado=empleado, desde=desde, hasta=hasta, fecha_pago=fecha_pago or ahora().date(),
                      metodo=metodo, nota=(nota or "").strip()[:255] or None, usuario_id=usuario_id,
                      deducciones_detalle=(deducciones_detalle or "").strip()[:255] or None,
                      **{k: c[k] for k in ("dias", "basico", "aux_transporte", "destajo", "horas_extra",
                                           "bonificaciones", "deduccion_salud", "deduccion_pension",
                                           "otras_deducciones", "neto", "aportes", "provisiones",
                                           "costo_empresa", "gasto")})
    db.session.add(pago)
    db.session.flush()
    for l in lineas:
        l.nomina_id = pago.id
    return pago


def anular(pago):
    if pago.anulada:
        raise ErrorNegocio("Esta nómina ya estaba anulada.")
    for l in ProduccionMO.query.filter_by(nomina_id=pago.id).all():
        l.nomina_id = None
    pago.anulada = True
