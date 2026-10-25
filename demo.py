"""Datos de ejemplo de una fábrica de calzado (60 días de operación) para una empresa.

Se usa desde el panel SoftGest al crear una demo, o por consola:
    python demo.py "Nombre de la empresa"
"""
import random
from datetime import date, datetime, timedelta

from models import (Activo, Cliente, Configuracion, DatosNegocio, Empleado, Gasto, Insumo, PrepRecetaItem,
                    ProcesoTarifa, Producto, RecetaItem, Usuario, Venta, ahora, db, empresa_id_actual)
import nomina as nom
from services import (ErrorNegocio, registrar_abono, registrar_compra, registrar_preparacion,
                      registrar_produccion, registrar_venta)

DIAS = 60


def cargar(empresa, admin):
    """Carga 60 días de operación en la empresa activa (set_empresa ya hecho). Devuelve los usuarios demo."""
    assert empresa_id_actual() == empresa.id
    if Producto.query.first():
        raise ErrorNegocio("Esta empresa ya tiene referencias. Vacía los datos antes de cargar la demo.")
    random.seed(11 + empresa.id)
    neg = DatosNegocio.actual()
    neg.ciudad = neg.ciudad or "Cúcuta"
    neg.telefono = neg.telefono or empresa.telefono
    usuarios = []
    for nombre, sufijo, rol in (("Marta (producción)", "produccion", "produccion"),
                                ("Julián (ventas)", "ventas", "vendedor")):
        user = f"{empresa.slug}.{sufijo}"
        u = Usuario.query.filter_by(username=user).first()
        if not u:
            u = Usuario(nombre=nombre, username=user, rol=rol, empresa_id=empresa.id)
            u.set_password("demo123")
            db.session.add(u)
        usuarios.append(u)
    db.session.commit()
    marta, julian = usuarios
    inicio = ahora().replace(hour=8, minute=0) - timedelta(days=DIAS)

    # nombre, categoría, unidad, mínimo, cantidad comprada, costo total, proveedor
    datos = [
        ("Cuero napa negro", "Cueros y sintéticos", "metro cuadrado", 8, 140, 11200000, "Curtiembre del Norte"),
        ("Cuero napa café", "Cueros y sintéticos", "metro cuadrado", 6, 84, 6720000, "Curtiembre del Norte"),
        ("Sintético charol negro", "Cueros y sintéticos", "metro", 15, 160, 2880000, "Sintéticos La 8"),
        ("Forro badana", "Forros", "metro cuadrado", 8, 205, 6560000, "Curtiembre del Norte"),
        ("Forro textil lona", "Forros", "metro", 10, 90, 810000, "Sintéticos La 8"),
        ("Suela TR dama", "Suelas y tacones", "par", 60, 700, 4550000, "Suelas Pamplonita"),
        ("Suela caucho caballero", "Suelas y tacones", "par", 40, 380, 3420000, "Suelas Pamplonita"),
        ("Suela escolar", "Suelas y tacones", "par", 40, 420, 2940000, "Suelas Pamplonita"),
        ("Cartón celulosa (lámina)", "Plantillas y refuerzos", "lámina", 5, 200, 2400000, "Insumos del Calzado"),
        ("Espuma 3 mm (lámina)", "Plantillas y refuerzos", "lámina", 5, 180, 2700000, "Insumos del Calzado"),
        ("Contrafuerte termoplástico", "Plantillas y refuerzos", "par", 80, 1800, 1080000, "Insumos del Calzado"),
        ("Puntera termoplástica", "Plantillas y refuerzos", "par", 80, 1800, 720000, "Insumos del Calzado"),
        ("Pegante PU", "Pegantes y químicos", "galón", 3, 20, 1700000, "Químicos Frontera"),
        ("Pegante bóxer", "Pegantes y químicos", "galón", 2, 14, 770000, "Químicos Frontera"),
        ("Halogenante (primer)", "Pegantes y químicos", "galón", 1, 6, 420000, "Químicos Frontera"),
        ("Hilo nylon", "Hilos", "metro", 3000, 60000, 280000, "Insumos del Calzado"),
        ("Cordones", "Herrajes y accesorios", "par", 80, 900, 315000, "Insumos del Calzado"),
        ("Ojaletes", "Herrajes y accesorios", "unidad", 600, 14000, 350000, "Insumos del Calzado"),
        ("Hebillas", "Herrajes y accesorios", "unidad", 60, 500, 400000, "Insumos del Calzado"),
        ("Cajas de cartón", "Empaque", "unidad", 120, 1800, 2160000, "Cajas y Empaques"),
        ("Papel seda", "Empaque", "unidad", 200, 3500, 280000, "Cajas y Empaques"),
    ]
    ins = {}
    for nombre, cat, unidad, minimo, cant, costo, prov in datos:
        i = Insumo(nombre=nombre, categoria=cat, unidad=unidad, stock_minimo=minimo)
        db.session.add(i)
        registrar_compra(i, cant, costo, prov, "Compra inicial", inicio, admin.id)
        ins[nombre] = i
    # Semielaborado: plantilla armada (por par)
    plantilla = Insumo(nombre="Plantilla armada", categoria="Plantillas y refuerzos", unidad="par",
                       stock_minimo=60, es_preparacion=True, tanda_rinde=60)
    db.session.add(plantilla)
    db.session.flush()
    for comp, c in (("Cartón celulosa (lámina)", 6), ("Espuma 3 mm (lámina)", 5), ("Forro badana", 3.4)):
        plantilla.receta_prep.append(PrepRecetaItem(componente=ins[comp], cantidad=c / 60,
                                                    unidad=ins[comp].unidad))
    db.session.commit()
    registrar_preparacion(plantilla, 1440, {ins["Cartón celulosa (lámina)"]: 144, ins["Espuma 3 mm (lámina)"]: 120,
                                           ins["Forro badana"]: 82}, mano_obra=720000,
                          fecha=inicio + timedelta(hours=3), usuario_id=marta.id)
    ins["Plantilla armada"] = plantilla
    db.session.commit()

    T = {"Corte": 0, "Guarnición (aparado)": 0, "Montaje": 0, "Soladura": 0, "Terminado / alistado": 0}
    # codigo, nombre, color, línea, material, tallas, detal, mayor, ficha por par, tarifas por proceso
    refs = [
        ("REF-101", "Baleta clásica", "Negro", "Dama", "Cuero napa", (35, 40), 95000, 68000,
         {"Cuero napa negro": 0.15, "Forro badana": 0.13, "Suela TR dama": 1, "Plantilla armada": 1,
          "Contrafuerte termoplástico": 1, "Puntera termoplástica": 1, "Pegante PU": 25 / 3785.41,
          "Halogenante (primer)": 5 / 3785.41, "Hilo nylon": 10, "Cajas de cartón": 1, "Papel seda": 2},
         [1800, 2500, 2200, 1500, 1000]),
        ("REF-101", "Baleta clásica", "Café", "Dama", "Cuero napa", (35, 40), 95000, 68000,
         {"Cuero napa café": 0.15, "Forro badana": 0.13, "Suela TR dama": 1, "Plantilla armada": 1,
          "Contrafuerte termoplástico": 1, "Puntera termoplástica": 1, "Pegante PU": 25 / 3785.41,
          "Halogenante (primer)": 5 / 3785.41, "Hilo nylon": 10, "Cajas de cartón": 1, "Papel seda": 2},
         [1800, 2500, 2200, 1500, 1000]),
        ("REF-205", "Botín casual", "Negro", "Caballero", "Cuero napa", (38, 43), 165000, 115000,
         {"Cuero napa negro": 0.26, "Forro badana": 0.22, "Suela caucho caballero": 1, "Plantilla armada": 1,
          "Contrafuerte termoplástico": 1, "Puntera termoplástica": 1, "Pegante PU": 35 / 3785.41,
          "Pegante bóxer": 10 / 3785.41, "Halogenante (primer)": 6 / 3785.41, "Hilo nylon": 18,
          "Cordones": 1, "Ojaletes": 12, "Cajas de cartón": 1, "Papel seda": 2},
         [2500, 4000, 3000, 2000, 1200]),
        ("REF-310", "Colegial escolar", "Negro", "Escolar", "Sintético charol", (27, 37), 75000, 52000,
         {"Sintético charol negro": 0.25, "Forro textil lona": 0.2, "Suela escolar": 1, "Plantilla armada": 1,
          "Contrafuerte termoplástico": 1, "Puntera termoplástica": 1, "Pegante bóxer": 20 / 3785.41,
          "Halogenante (primer)": 4 / 3785.41, "Hilo nylon": 10, "Cordones": 1, "Ojaletes": 10,
          "Cajas de cartón": 1, "Papel seda": 1},
         [1200, 2000, 1800, 1200, 800]),
        ("REF-415", "Sandalia tiras", "Café", "Dama", "Cuero napa", (35, 40), 79000, 55000,
         {"Cuero napa café": 0.093, "Forro badana": 0.075, "Suela TR dama": 1, "Plantilla armada": 1,
          "Pegante PU": 20 / 3785.41, "Halogenante (primer)": 4 / 3785.41, "Hilo nylon": 6, "Hebillas": 2,
          "Cajas de cartón": 1, "Papel seda": 1},
         [1200, 1800, 1500, 0, 800]),
    ]
    prods = []
    for cod, nombre, color, linea, mat, (tmin, tmax), pv, pm, ficha, tarifas in refs:
        p = Producto(codigo=cod, nombre=nombre, color=color, linea=linea, material=mat, talla_min=tmin,
                     talla_max=tmax, precio_venta=pv, precio_mayor=pm, stock_minimo=12, tanda_rinde=1)
        db.session.add(p)
        for n_ins, c in ficha.items():
            p.receta.append(RecetaItem(insumo=ins[n_ins], cantidad=c,
                                       unidad={"galón": "mililitro", "metro cuadrado": "decímetro cuadrado"}.get(ins[n_ins].unidad, ins[n_ins].unidad)))
        for proc, v in zip(T, tarifas):
            if v:
                p.procesos.append(ProcesoTarifa(proceso=proc, valor_par=v))
        prods.append(p)
    db.session.commit()

    emps = []
    for nombre, proc, tipo, sal in (("Jhon Jairo Peña", "Corte", "destajo", 0),
                                    ("Luz Marina Ortega", "Guarnición (aparado)", "destajo", 0),
                                    ("Yesenia Rolón", "Guarnición (aparado)", "destajo", 0),
                                    ("Carlos Andrés Mora", "Montaje", "destajo", 0),
                                    ("Wílmer Duarte", "Soladura", "destajo", 0),
                                    ("Diana Ruiz", "Terminado / alistado", "mixto", 900000),
                                    ("Patricia Gómez", None, "fijo", 2000000)):
        e = Empleado(nombre=nombre, proceso=proc, tipo_pago=tipo, salario=sal, aux_transporte=True,
                     cargo="Auxiliar administrativa y ventas" if proc is None else proc.split(" ")[0],
                     fecha_ingreso=(inicio - timedelta(days=random.randint(90, 900))).date(),
                     documento=str(random.randint(1090000000, 1099999999)),
                     telefono=f"31{random.randint(0, 9)} {random.randint(100, 999)} {random.randint(1000, 9999)}")
        db.session.add(e)
        emps.append(e)
    db.session.commit()
    por_proc = {}
    for e in emps:
        if e.proceso:
            por_proc.setdefault(e.proceso, []).append(e)

    for nombre, valor, meses, dias in (("Máquina guarnecedora poste #1", 2800000, 60, 400),
                                       ("Máquina guarnecedora plana #2", 2400000, 60, 300),
                                       ("Troqueladora hidráulica", 6500000, 84, 500),
                                       ("Desbastadora", 3200000, 60, 250),
                                       ("Hormas (juego de 60 pares)", 1500000, 36, 200),
                                       ("Compresor y pistola", 1200000, 48, 120)):
        db.session.add(Activo(nombre=nombre, valor=valor, valor_residual=round(valor * .1), vida_util_meses=meses,
                              fecha_compra=(inicio - timedelta(days=dias)).date(), proveedor="Maquinaria del Oriente"))
    db.session.commit()

    clientes = []
    for nombre, tipo, ciudad, plazo, mayor, cupo in (
            ("Almacén El Paso", "Almacén / tienda", "Cúcuta", 30, True, 6000000),
            ("Calzado La 11", "Almacén / tienda", "Bucaramanga", 45, True, 9000000),
            ("Distribuidora Frontera", "Distribuidor", "Cúcuta", 30, True, 12000000),
            ("Boutique Andrea", "Almacén / tienda", "Pamplona", 15, True, None),
            ("Sandra Villamizar", "Vendedor por catálogo", "Villa del Rosario", 15, True, 1500000),
            ("Colegio San Luis (dotación)", "Empresa (dotación)", "Cúcuta", 30, True, None),
            ("Karen Quintero", "Particular", "Cúcuta", 0, False, None),
            ("Oscar Ramírez", "Particular", "Los Patios", 0, False, None)):
        c = Cliente(nombre=nombre, tipo=tipo, ciudad=ciudad, plazo_dias=plazo, usa_precio_mayor=mayor, cupo=cupo,
                    telefono=f"30{random.randint(0, 9)} {random.randint(100, 999)} {random.randint(1000, 9999)}",
                    identificacion=str(random.randint(80000000, 1099999999)))
        db.session.add(c)
        clientes.append(c)
    db.session.commit()
    mayoristas, particulares = clientes[:6], clientes[6:]

    def corrida(p, total):
        ts = p.rango_tallas
        n = len(ts)
        pesos = [max(n - abs(i - (n - 1) / 2) * 1.6, .5) for i in range(n)]
        s = sum(pesos)
        rep = [round(w / s * total) for w in pesos]
        rep[len(rep) // 2] += total - sum(rep)
        return {t: c for t, c in zip(ts, rep) if c > 0}

    def producir(p, docenas, fecha):
        cor = corrida(p, 12 * docenas)
        q = sum(cor.values())
        cons = {r.insumo: r.cantidad * q * (1 if r.insumo.unidad in ("par", "unidad") else random.uniform(1.0, 1.06))
                for r in p.receta}
        mo = []
        for pt in p.procesos:
            opciones = por_proc.get(pt.proceso) or []
            e = random.choice(opciones) if opciones else None
            mo.append(dict(proceso=pt.proceso, empleado=e, responsable=None if e else "Taller externo",
                           modo="par", valor=pt.valor_par, pares=q))
        return registrar_produccion(p, cor, cons, mo, energia=0, transporte=random.choice([0, 0, 15000]),
                                    fecha=fecha, usuario_id=marta.id, nota=None)

    for d in range(DIAS):
        dia = inicio + timedelta(days=d)
        # Reposición de materiales cada 20 días
        if d and d % 20 == 0:
            for nombre, cat, unidad, minimo, cant, costo, prov in datos:
                registrar_compra(ins[nombre], cant * .6, costo * .6 * random.uniform(1, 1.04), prov, "Reposición",
                                 dia.replace(hour=7), admin.id)
            registrar_preparacion(plantilla, 720, {ins["Cartón celulosa (lámina)"]: 72, ins["Espuma 3 mm (lámina)"]: 60,
                                                   ins["Forro badana"]: 41}, mano_obra=360000,
                                  fecha=dia.replace(hour=7, minute=30), usuario_id=marta.id)
            db.session.commit()
        # Producción: casi todos los días hábiles un lote de 1–3 docenas
        if dia.weekday() < 6:
            for p in random.sample(prods, 2 if d % 3 else 1):
                try:
                    producir(p, random.randint(1, 3), dia.replace(hour=16))
                except ErrorNegocio:
                    db.session.rollback()
        db.session.commit()
        # Ventas
        if d < 5:
            continue
        for _ in range(random.randint(1, 3)):
            mayor = random.random() < .55
            cli = random.choice(mayoristas if mayor else particulares + [None, None])
            disp = [p for p in prods if p.stock > 0]
            if not disp:
                break
            items = []
            for p in random.sample(disp, min(len(disp), random.randint(1, 3 if mayor else 1))):
                con = [t for t, s in p.stock_por_talla.items() if s > 0]
                for t in random.sample(con, min(len(con), random.randint(1, 4 if mayor else 1))):
                    q = min(p.stock_talla(t), random.randint(2, 6) if mayor else 1)
                    items.append((p, t, q, p.precio_mayor if mayor else p.precio_venta))
            if not items:
                continue
            credito = bool(cli and cli.plazo_dias and mayor and random.random() < .8)
            try:
                v = registrar_venta(items, cli.nombre if cli else "", random.choice(["Efectivo", "Nequi", "Transferencia"]),
                                    fecha=dia.replace(hour=random.randint(9, 18), minute=random.randint(0, 59)),
                                    usuario_id=random.choice([julian.id, admin.id]), cliente_id=cli.id if cli else None,
                                    condicion="credito" if credito else "contado", plazo_dias=cli.plazo_dias if cli else 0,
                                    descuento=0, iva_pct=0,
                                    abono_inicial=round(sum(c * pr for _, _, c, pr in items) * .3, -3) if credito and random.random() < .4 else 0)
                v.cliente_doc = cli.identificacion if cli else None
                db.session.commit()
            except ErrorNegocio:
                db.session.rollback()
        # Abonos de cartera
        for v in Venta.query.filter(Venta.condicion == "credito", Venta.anulada.is_(False), Venta.fecha < dia).all():
            if v.saldo > 0.5 and random.random() < .05:
                valor = v.saldo if random.random() < .5 else round(v.saldo / 2, -3)
                try:
                    registrar_abono(v, valor, random.choice(["Transferencia", "Efectivo"]), "", dia.replace(hour=11),
                                    admin.id)
                    db.session.commit()
                except ErrorNegocio:
                    db.session.rollback()

    # Gastos fijos y servicios de cada mes
    mes = inicio.date().replace(day=1)
    hoy = ahora().date()
    while mes <= hoy:
        for dia_m, cat, desc, val in ((1, "Arriendo", "Arriendo del taller", 1800000),
                                      (12, "Energía eléctrica", "Recibo de energía", random.randint(380000, 460000)),
                                      (14, "Agua y alcantarillado", "Recibo de agua", random.randint(80000, 110000)),
                                      (5, "Internet / celular", "Plan internet + celular", 95000),
                                      (20, "Honorarios (contador, asesorías)", "Contador del mes", 450000),
                                      (25, "Transporte / fletes", "Envíos a Bucaramanga", random.randint(120000, 220000))):
            f = mes.replace(day=dia_m)
            if inicio.date() <= f <= hoy:
                db.session.add(Gasto(fecha=f, categoria=cat, descripcion=desc, valor=val, usuario_id=admin.id))
        mes = (mes.replace(day=28) + timedelta(days=4)).replace(day=1)
    db.session.commit()

    # Nómina por quincenas ya cerradas
    cfg = Configuracion.actual()
    q_ini = inicio.date().replace(day=1)
    while True:
        q_fin = q_ini.replace(day=15) if q_ini.day == 1 else (q_ini.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        if q_fin >= hoy:
            break
        for e in emps:
            if q_fin < inicio.date():
                continue
            try:
                nom.registrar(e, cfg, max(q_ini, inicio.date()), q_fin, q_fin + timedelta(days=1),
                              15 if q_ini >= inicio.date() else (q_fin - inicio.date()).days + 1,
                              seguridad_social=e.tipo_pago != "destajo", prestaciones=e.tipo_pago != "destajo",
                              metodo="Transferencia", usuario_id=admin.id)
            except ErrorNegocio:
                db.session.rollback()
        db.session.commit()
        q_ini = q_fin + timedelta(days=1)

    return marta.username, julian.username


if __name__ == "__main__":
    import sys
    from app import app, crear_empresa
    from models import set_empresa
    with app.app_context():
        nombre = sys.argv[1] if len(sys.argv) > 1 else "Calzado Demo"
        emp, adm = crear_empresa(nombre, "demo", 15, None, "admin123")
        set_empresa(emp.id)
        print("Empresa", emp.nombre, "· admin:", adm.username, "/ admin123 · usuarios demo:", cargar(emp, adm))
