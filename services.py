"""Lógica de negocio: compras, producción por tallas y procesos, ventas, cartera y ajustes.

Cada función valida, mueve inventario y calcula costos. Si algo no cuadra
lanza ErrorNegocio con la lista de mensajes para mostrar al usuario.
El commit lo hace quien llama.
"""
from datetime import timedelta

from models import (MODOS_MO, MOTIVOS_AJUSTE, Abono, Ajuste, AjusteItem, CompraInsumo, Devolucion,
                    DevolucionItem, Insumo, Preparacion, PreparacionConsumo, Produccion,
                    ProduccionConsumo, ProduccionMO, ProduccionTalla, Producto, ProductoTalla, Venta,
                    VentaItem, ahora, db)

TOL = 1e-9


class ErrorNegocio(Exception):
    def __init__(self, errores):
        self.errores = errores if isinstance(errores, list) else [errores]
        super().__init__("; ".join(self.errores))


def promedio_ponderado(stock, costo_actual, cantidad, costo_unitario):
    """Costo promedio ponderado al recibir una entrada de inventario."""
    stock = stock or 0
    nuevo = stock + cantidad
    if stock <= 0 or nuevo <= 0:
        return costo_unitario
    return (stock * (costo_actual or 0) + cantidad * costo_unitario) / nuevo


def _quitar_de_promedio(stock, costo_prom, cantidad, costo_unit):
    """Inverso del promedio ponderado: saca una entrada del costo promedio."""
    restante = (stock or 0) - cantidad
    if restante <= TOL:
        return costo_prom
    return max(((stock or 0) * (costo_prom or 0) - cantidad * costo_unit) / restante, 0)


def mover_talla(producto, talla, delta):
    """Suma (o resta) pares a una talla y al total de la referencia."""
    talla = int(talla)
    fila = next((pt for pt in producto.tallas_stock if pt.talla == talla), None)
    if fila is None:
        fila = ProductoTalla(talla=talla, stock=0)
        producto.tallas_stock.append(fila)
    fila.stock = (fila.stock or 0) + int(delta)
    producto.stock = (producto.stock or 0) + int(delta)


# ----------------------------------------------------------------- compras
def registrar_compra(insumo, cantidad, costo_total, proveedor="", nota="",
                     fecha=None, usuario_id=None):
    if cantidad <= 0:
        raise ErrorNegocio(f"La cantidad de «{insumo.nombre}» debe ser mayor a 0.")
    if costo_total < 0:
        raise ErrorNegocio(f"El costo de «{insumo.nombre}» no puede ser negativo.")
    costo_unitario = costo_total / cantidad
    insumo.costo_promedio = promedio_ponderado(
        insumo.stock, insumo.costo_promedio, cantidad, costo_unitario)
    insumo.stock = (insumo.stock or 0) + cantidad
    compra = CompraInsumo(
        fecha=fecha or ahora(), insumo=insumo, cantidad=cantidad,
        costo_total=costo_total, costo_unitario=costo_unitario,
        proveedor=proveedor or None, nota=nota or None, usuario_id=usuario_id,
    )
    db.session.add(compra)
    return compra


def anular_compra(compra):
    if compra.anulada:
        raise ErrorNegocio("Esta compra ya estaba anulada.")
    ins = compra.insumo
    if (ins.stock or 0) + TOL < compra.cantidad:
        raise ErrorNegocio(
            f"No se puede anular: de «{ins.nombre}» quedan {ins.stock:g} {ins.unidad} y la compra fue de "
            f"{compra.cantidad:g}. Parte ya se usó en producción; anula primero esas producciones.")
    ins.costo_promedio = _quitar_de_promedio(ins.stock, ins.costo_promedio, compra.cantidad,
                                             compra.costo_unitario)
    ins.stock = max((ins.stock or 0) - compra.cantidad, 0)
    compra.anulada = True


# --------------------------------------------------------------- producción
def total_mano_obra(modo, valor, pares):
    if modo == "par":
        return valor * pares
    if modo == "docena":
        return valor * pares / 12
    return valor


def registrar_produccion(producto, corrida, consumos, mano_obra, energia=0.0, gas_agua=0.0,
                         transporte=0.0, otros=0.0, otros_descripcion="", nota="", fecha=None,
                         usuario_id=None, sin_materiales=False):
    """corrida: {talla: pares}. consumos: {Insumo: cantidad}.
    mano_obra: lista de dicts {proceso, empleado (Empleado|None), responsable, modo, valor, pares}."""
    errores = []
    if not producto:
        errores.append("Selecciona la referencia que vas a producir.")
    corrida = {int(t): int(c) for t, c in (corrida or {}).items() if int(c or 0) > 0}
    cantidad = sum(corrida.values())
    if cantidad <= 0:
        errores.append("Escribe cuántos pares salieron de cada talla (la corrida).")
    if any(c < 0 for c in corrida.values()):
        errores.append("Las cantidades por talla no pueden ser negativas.")
    consumos = {i: c for i, c in consumos.items() if c > 0}
    if not consumos and not sin_materiales:
        errores.append("Registra al menos un material utilizado.")
    for insumo, cant in consumos.items():
        if cant > (insumo.stock or 0) + TOL:
            errores.append(
                f"No hay suficiente «{insumo.nombre}»: necesitas "
                f"{cant:g} {insumo.unidad} y hay {insumo.stock:g}.")
    lineas = []
    for ln in mano_obra or []:
        valor = ln.get("valor") or 0
        if valor < 0:
            errores.append(f"El valor de «{ln.get('proceso') or 'mano de obra'}» no puede ser negativo.")
            continue
        if not ln.get("proceso") or valor == 0:
            continue
        modo = ln.get("modo") if ln.get("modo") in MODOS_MO else "par"
        pares = int(ln.get("pares") or cantidad)
        if pares <= 0:
            pares = cantidad
        lineas.append(dict(ln, modo=modo, pares=pares, total=total_mano_obra(modo, valor, pares)))
    for etiqueta, v in (("energía", energia), ("agua/gas", gas_agua),
                        ("transporte", transporte), ("otros", otros)):
        if v < 0:
            errores.append(f"El costo de {etiqueta} no puede ser negativo.")
    if otros > 0 and not (otros_descripcion or "").strip():
        errores.append("Aclara en qué consisten los «otros» costos.")
    if errores:
        raise ErrorNegocio(errores)

    p = Produccion(
        fecha=fecha or ahora(), producto=producto, cantidad=cantidad,
        costo_energia=energia, costo_gas_agua=gas_agua,
        costo_transporte=transporte, costo_otros=otros,
        otros_descripcion=(otros_descripcion or "").strip() or None,
        nota=nota or None, usuario_id=usuario_id,
    )
    costo_insumos = 0.0
    for insumo, cant in consumos.items():
        cu = insumo.costo_promedio or 0
        total = cant * cu
        costo_insumos += total
        insumo.stock = (insumo.stock or 0) - cant
        p.consumos.append(ProduccionConsumo(
            insumo=insumo, cantidad=cant, costo_unitario=cu, costo_total=total))
    costo_mo = 0.0
    for ln in lineas:
        emp = ln.get("empleado")
        p.mano_obra.append(ProduccionMO(
            proceso=ln["proceso"][:40], empleado=emp,
            responsable=None if emp else ((ln.get("responsable") or "").strip()[:120] or None),
            modo=ln["modo"], valor=ln["valor"], pares=ln["pares"], total=ln["total"]))
        costo_mo += ln["total"]
    for t, c in sorted(corrida.items()):
        p.tallas.append(ProduccionTalla(talla=t, cantidad=c))

    p.costo_insumos = costo_insumos
    p.costo_mano_obra = costo_mo
    p.costo_total = costo_insumos + costo_mo + p.costo_indirectos
    p.costo_unitario = p.costo_total / cantidad

    producto.costo_promedio = promedio_ponderado(
        producto.stock, producto.costo_promedio, cantidad, p.costo_unitario)
    for t, c in corrida.items():
        mover_talla(producto, t, c)
    db.session.add(p)
    return p


def anular_produccion(prod):
    if prod.anulada:
        raise ErrorNegocio("Esta producción ya estaba anulada.")
    p = prod.producto
    errores = []
    for t in prod.tallas:
        hay = p.stock_talla(t.talla)
        if hay < t.cantidad:
            errores.append(f"Talla {t.talla}: quedan {hay} pares y esta producción fue de {t.cantidad}. "
                           "Algunos ya se vendieron; anula primero esas ventas.")
    pagadas = {m.nomina.numero for m in prod.mano_obra if m.nomina_id and m.nomina}
    if pagadas:
        errores.append("La mano de obra de este lote ya se pagó en nómina "
                       f"({', '.join(sorted(pagadas))}). Anula primero esa nómina.")
    if errores:
        raise ErrorNegocio(errores)
    p.costo_promedio = _quitar_de_promedio(p.stock, p.costo_promedio, prod.cantidad, prod.costo_unitario)
    for t in prod.tallas:
        mover_talla(p, t.talla, -t.cantidad)
    for c in prod.consumos:
        ins = c.insumo
        ins.costo_promedio = promedio_ponderado(ins.stock, ins.costo_promedio, c.cantidad, c.costo_unitario)
        ins.stock = (ins.stock or 0) + c.cantidad
    prod.anulada = True


# ------------------------------------------------------------------- ventas
def registrar_venta(items, cliente="", metodo_pago="Efectivo", nota="", fecha=None, usuario_id=None,
                    cliente_id=None, condicion="contado", plazo_dias=0, descuento=0.0, iva_pct=0.0,
                    abono_inicial=0.0):
    """items: lista de (Producto, talla, cantidad, precio_unitario)."""
    errores = []
    agrupado = {}
    for producto, talla, cant, precio in items:
        if not producto or cant <= 0:
            continue
        if talla is None:
            errores.append(f"Elige la talla de «{producto.etiqueta}».")
            continue
        if precio < 0:
            errores.append(f"El precio de «{producto.etiqueta}» no puede ser negativo.")
        clave = (producto, int(talla), round(precio, 2))
        agrupado[clave] = agrupado.get(clave, 0) + cant
    if not agrupado and not errores:
        errores.append("Agrega al menos un par a la venta.")
    por_talla = {}
    for (producto, talla, _), cant in agrupado.items():
        por_talla[(producto, talla)] = por_talla.get((producto, talla), 0) + cant
    for (producto, talla), cant in por_talla.items():
        hay = producto.stock_talla(talla)
        if cant > hay:
            errores.append(f"De «{producto.etiqueta}» talla {talla} hay {hay} pares y quieres vender {cant}.")
    condicion = "credito" if condicion == "credito" else "contado"
    if condicion == "credito" and not (cliente or "").strip():
        errores.append("Para vender a crédito escribe o elige el cliente.")
    subtotal = sum(c * k[2] for k, c in agrupado.items())
    if descuento < 0 or descuento > subtotal + TOL:
        errores.append("El descuento debe estar entre $0 y el subtotal de la venta.")
    if iva_pct < 0 or iva_pct > 100:
        errores.append("El porcentaje de IVA no es válido.")
    if errores:
        raise ErrorNegocio(errores)

    fecha = fecha or ahora()
    v = Venta(fecha=fecha, cliente=(cliente or "").strip() or None, cliente_id=cliente_id,
              metodo_pago=metodo_pago, nota=nota or None, usuario_id=usuario_id,
              condicion=condicion, iva_pct=iva_pct, descuento=descuento)
    if condicion == "credito":
        v.vence = fecha.date() + timedelta(days=max(int(plazo_dias or 0), 0))
    costo = 0.0
    for (producto, talla, precio), cant in agrupado.items():
        cu = producto.costo_promedio or 0
        it = VentaItem(producto=producto, talla=talla, cantidad=cant, precio_unitario=precio,
                       costo_unitario=cu, subtotal=cant * precio, costo=cant * cu)
        v.items.append(it)
        costo += it.costo
        mover_talla(producto, talla, -cant)
    _recalcular_venta(v, costo)
    if condicion == "credito" and abono_inicial > 0:
        if abono_inicial > v.total_factura + 0.5:
            raise ErrorNegocio("El abono inicial no puede ser mayor que el total de la venta.")
        v.abonos.append(Abono(fecha=fecha, valor=abono_inicial, metodo=metodo_pago,
                              nota="Abono inicial", usuario_id=usuario_id))
    db.session.add(v)
    return v


def _recalcular_venta(v, costo=None):
    sub = sum(i.subtotal for i in v.items)
    v.descuento = min(v.descuento or 0, sub)
    v.total = sub - (v.descuento or 0)
    v.iva = round(v.total * (v.iva_pct or 0) / 100, 0)
    v.costo_total = costo if costo is not None else sum(i.costo for i in v.items)
    v.ganancia = v.total - v.costo_total


def anular_venta(venta):
    if venta.anulada:
        raise ErrorNegocio("Esta venta ya estaba anulada.")
    for it in venta.items:
        p = it.producto
        p.costo_promedio = promedio_ponderado(p.stock, p.costo_promedio, it.cantidad, it.costo_unitario)
        mover_talla(p, it.talla or 0, it.cantidad)
    for a in venta.abonos:
        a.anulado = True
    venta.anulada = True


def registrar_abono(venta, valor, metodo="Efectivo", nota="", fecha=None, usuario_id=None):
    if venta.anulada:
        raise ErrorNegocio("La venta está anulada; no recibe abonos.")
    if venta.condicion != "credito":
        raise ErrorNegocio("Esta venta fue de contado: ya está pagada.")
    if valor <= 0:
        raise ErrorNegocio("El valor del abono debe ser mayor a 0.")
    if valor > venta.saldo + 0.5:
        raise ErrorNegocio(f"El abono supera el saldo pendiente (${venta.saldo:,.0f}).".replace(",", "."))
    a = Abono(fecha=fecha or ahora(), valor=valor, metodo=metodo, nota=(nota or "").strip()[:255] or None,
              usuario_id=usuario_id)
    venta.abonos.append(a)
    return a


def devolver_parcial(venta, cantidades, motivo="", usuario_id=None):
    """cantidades: {VentaItem: pares que devuelve}. Regresan a su talla y la venta se ajusta."""
    if venta.anulada:
        raise ErrorNegocio("Esta venta está anulada; no admite devoluciones.")
    cantidades = {it: c for it, c in cantidades.items() if c > 0}
    if not cantidades:
        raise ErrorNegocio("Indica cuántos pares devuelve el cliente.")
    errores = [f"De «{it.producto.etiqueta}» T{it.talla} se vendieron {it.cantidad}; no se pueden devolver {c}."
               for it, c in cantidades.items() if c > it.cantidad]
    if errores:
        raise ErrorNegocio(errores)
    if sum(cantidades.values()) >= venta.unidades:
        raise ErrorNegocio("Si el cliente devuelve todo, usa «Anular venta».")

    sub_antes = venta.subtotal or 1
    dev = Devolucion(venta_id=venta.id, motivo=(motivo or "").strip()[:255] or None, usuario_id=usuario_id,
                     total=0.0, unidades=0)
    for it, c in cantidades.items():
        p = it.producto
        p.costo_promedio = promedio_ponderado(p.stock, p.costo_promedio, c, it.costo_unitario)
        mover_talla(p, it.talla or 0, c)
        dev.items.append(DevolucionItem(producto=p, talla=it.talla, cantidad=c,
                                        precio_unitario=it.precio_unitario))
        dev.total += c * it.precio_unitario
        dev.unidades += c
        it.cantidad -= c
        it.subtotal = it.cantidad * it.precio_unitario
        it.costo = it.cantidad * it.costo_unitario
        if it.cantidad == 0:
            venta.items.remove(it)
    sub_despues = sum(i.subtotal for i in venta.items)
    venta.descuento = round((venta.descuento or 0) * sub_despues / sub_antes, 0)
    _recalcular_venta(venta)
    db.session.add(dev)
    return dev


# ------------------------------------------------------------ semielaborados
def registrar_preparacion(prep_insumo, cantidad, consumos, mano_obra=0.0, otros=0.0,
                          otros_descripcion="", nota="", fecha=None, usuario_id=None):
    """Elabora un semielaborado propio. consumos: dict {Insumo: cantidad_usada}."""
    errores = []
    if not prep_insumo or not prep_insumo.es_preparacion:
        errores.append("Selecciona el semielaborado que elaboraste.")
    if cantidad <= 0:
        errores.append("La cantidad obtenida debe ser mayor a 0.")
    consumos = {i: c for i, c in consumos.items() if c > 0}
    if not consumos:
        errores.append("Registra al menos un insumo utilizado.")
    for insumo, cant in consumos.items():
        if prep_insumo and insumo.id == prep_insumo.id:
            errores.append("Un semielaborado no puede usarse a sí mismo como material.")
        elif cant > (insumo.stock or 0) + TOL:
            errores.append(f"No hay suficiente «{insumo.nombre}»: necesitas {cant:g} {insumo.unidad} "
                           f"y hay {insumo.stock:g}.")
    if mano_obra < 0 or otros < 0:
        errores.append("Los costos no pueden ser negativos.")
    if otros > 0 and not (otros_descripcion or "").strip():
        errores.append("Aclara en qué consisten los otros costos.")
    if errores:
        raise ErrorNegocio(errores)

    p = Preparacion(fecha=fecha or ahora(), insumo=prep_insumo, cantidad=cantidad,
                    costo_mano_obra=mano_obra, costo_otros=otros,
                    otros_descripcion=(otros_descripcion or "").strip() or None,
                    nota=nota or None, usuario_id=usuario_id)
    total_ins = 0.0
    for insumo, cant in consumos.items():
        cu = insumo.costo_promedio or 0
        total_ins += cant * cu
        insumo.stock = (insumo.stock or 0) - cant
        p.consumos.append(PreparacionConsumo(insumo=insumo, cantidad=cant, costo_unitario=cu,
                                             costo_total=cant * cu))
    p.costo_insumos = total_ins
    p.costo_total = total_ins + mano_obra + otros
    p.costo_unitario = p.costo_total / cantidad
    prep_insumo.costo_promedio = promedio_ponderado(prep_insumo.stock, prep_insumo.costo_promedio,
                                                    cantidad, p.costo_unitario)
    prep_insumo.stock = (prep_insumo.stock or 0) + cantidad
    db.session.add(p)
    return p


def anular_preparacion(p):
    if p.anulada:
        raise ErrorNegocio("Esta elaboración ya estaba anulada.")
    ins = p.insumo
    if (ins.stock or 0) + TOL < p.cantidad:
        raise ErrorNegocio(
            f"No se puede anular: de «{ins.nombre}» quedan {ins.stock:g} {ins.unidad} y esta elaboración "
            f"fue de {p.cantidad:g}. Parte ya se usó en producción; anula primero esas producciones.")
    ins.costo_promedio = _quitar_de_promedio(ins.stock, ins.costo_promedio, p.cantidad, p.costo_unitario)
    ins.stock = max((ins.stock or 0) - p.cantidad, 0)
    for c in p.consumos:
        i = c.insumo
        i.costo_promedio = promedio_ponderado(i.stock, i.costo_promedio, c.cantidad, c.costo_unitario)
        i.stock = (i.stock or 0) + c.cantidad
    p.anulada = True


# ------------------------------------------------------------------ ajustes
def registrar_ajuste(lineas, motivo, descripcion="", fecha=None, usuario_id=None):
    """Documento de egreso. lineas: lista de (Insumo|Producto, talla|None, cantidad que sale)."""
    errores = []
    if motivo not in MOTIVOS_AJUSTE:
        errores.append("Elige el motivo del egreso.")
    descripcion = (descripcion or "").strip()[:255]
    if motivo == "Otro" and not descripcion:
        errores.append("Si el motivo es «Otro», escribe una descripción.")
    agrupado = {}
    for obj, talla, cant in lineas:
        if obj is None or not cant:
            continue
        if cant < 0:
            errores.append(f"La cantidad de «{obj.nombre}» no puede ser negativa.")
            continue
        if isinstance(obj, Producto):
            if talla is None:
                errores.append(f"Elige la talla de «{obj.etiqueta}».")
                continue
            if abs(cant - round(cant)) > TOL:
                errores.append(f"De «{obj.etiqueta}» la cantidad debe ser un número entero de pares.")
                continue
            clave = (obj, int(talla))
        else:
            clave = (obj, None)
        agrupado[clave] = agrupado.get(clave, 0) + cant
    if not agrupado and not errores:
        errores.append("Agrega al menos un material o par con la cantidad que sale.")
    for (obj, talla), cant in agrupado.items():
        if isinstance(obj, Insumo):
            if cant > (obj.stock or 0) + TOL:
                errores.append(f"De «{obj.nombre}» hay {(obj.stock or 0):g} {obj.unidad}; no pueden salir {cant:g}.")
        elif cant > obj.stock_talla(talla):
            errores.append(f"De «{obj.etiqueta}» talla {talla} hay {obj.stock_talla(talla)} pares; "
                           f"no pueden salir {cant:g}.")
    if errores:
        raise ErrorNegocio(errores)

    a = Ajuste(fecha=fecha or ahora(), motivo=motivo, descripcion=descripcion or None,
               usuario_id=usuario_id, total=0.0)
    for (obj, talla), cant in agrupado.items():
        cu = obj.costo_promedio or 0
        if isinstance(obj, Insumo):
            it = AjusteItem(insumo=obj, cantidad=cant, stock_antes=obj.stock or 0, costo_unitario=cu,
                            costo_total=cant * cu)
            obj.stock = max((obj.stock or 0) - cant, 0)
        else:
            cant = int(round(cant))
            it = AjusteItem(producto=obj, talla=talla, cantidad=cant, stock_antes=obj.stock_talla(talla),
                            costo_unitario=cu, costo_total=cant * cu)
            mover_talla(obj, talla, -cant)
        a.items.append(it)
        a.total += it.costo_total
    db.session.add(a)
    return a


def anular_ajuste(a):
    if a.anulada:
        raise ErrorNegocio("Este egreso ya estaba anulado.")
    for it in a.items:
        obj = it.item
        if obj is None:
            continue
        obj.costo_promedio = promedio_ponderado(obj.stock, obj.costo_promedio, it.cantidad, it.costo_unitario)
        if it.producto:
            mover_talla(obj, it.talla or 0, int(it.cantidad))
        else:
            obj.stock = (obj.stock or 0) + it.cantidad
    a.anulada = True
