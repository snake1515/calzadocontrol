"""Recibos de venta en PDF (para PC) y JPG (para celular), egresos y desprendibles de nómina.

No es una factura electrónica DIAN: es un soporte de la venta para el cliente.
"""
import io
import os

from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

FONT_DIR = os.path.join(os.path.dirname(__file__), "static", "fonts")
F_REG = os.path.join(FONT_DIR, "DejaVuSans.ttf")
F_BOLD = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")
LOGO = os.path.join(os.path.dirname(__file__), "static", "logo.jpg")
HAY_LOGO = os.path.exists(LOGO)
LOGO_RATIO = 1.0
if HAY_LOGO:
    with Image.open(LOGO) as _im:
        LOGO_RATIO = _im.height / _im.width
pdfmetrics.registerFont(TTFont("DV", F_REG))
pdfmetrics.registerFont(TTFont("DVB", F_BOLD))

TINTA = "#14233a"
GRIS = "#5f6f84"
LINEA = "#d9e2ea"
ACENTO = "#b5602c"
ACENTO_SOFT = "#f8ede4"
ROJO = "#c53030"
NOTA_LEGAL = "Documento soporte de venta. No es factura electrónica de venta."


def numero(venta, prefijo="CZ"):
    return f"{(prefijo or 'CZ').strip().upper()}-{venta.id:06d}"


def cop(v):
    v = v or 0
    return ("-$ " if v < -0.5 else "$ ") + f"{abs(v):,.0f}".replace(",", ".")


def _datos(venta, negocio):
    lineas_neg = _lineas_negocio(negocio)
    items = [dict(nombre=f"{i.producto.etiqueta} · Talla {i.talla}", cant=i.cantidad, precio=i.precio_unitario,
                  subtotal=i.subtotal) for i in venta.items]
    credito = venta.condicion == "credito"
    totales = [("Subtotal", venta.subtotal)]
    if venta.descuento:
        totales.append(("Descuento", -venta.descuento))
    if venta.iva:
        totales.append((f"IVA {venta.iva_pct:g}%", venta.iva))
    return dict(
        negocio=negocio.nombre or "Mi negocio", lineas_neg=lineas_neg,
        titulo=(negocio.titulo_doc or "Recibo de venta").upper(),
        numero=numero(venta, negocio.prefijo), fecha=venta.fecha.strftime("%d/%m/%Y %I:%M %p"),
        cliente=venta.cliente or "Cliente general", doc=venta.cliente_doc or "",
        pago=("Crédito · vence " + venta.vence.strftime("%d/%m/%Y")) if credito and venta.vence else (venta.metodo_pago or ""),
        vendedor=venta.usuario.nombre if venta.usuario else "", items=items, totales=totales,
        unidades=venta.unidades, total=venta.total_factura, anulada=venta.anulada,
        abonado=venta.pagado if credito else None, saldo=venta.saldo if credito else None,
        mensaje=negocio.mensaje or "", nota=venta.nota or "",
    )


def _lineas_negocio(negocio):
    return [x for x in (
        " · ".join(x for x in (f"NIT {negocio.nit}" if negocio.nit else None, negocio.regimen) if x) or None,
        " · ".join(x for x in (negocio.direccion, negocio.ciudad) if x) or None,
        " · ".join(x for x in (f"Tel. {negocio.telefono}" if negocio.telefono else None, negocio.email) if x) or None,
    ) if x]


# --------------------------------------------------------------------- PDF
def _encabezado_pdf(c, d, titulo, x0, x1, y):
    """Logo, datos del negocio, título, número y fecha. Devuelve la y más baja usada."""
    W, H = letter
    # Encabezado
    c.setFillColor(HexColor(ACENTO))
    c.rect(0, H - 8 * mm, W, 8 * mm, stroke=0, fill=1)
    xt = x0
    if HAY_LOGO:
        lw = 30 * mm
        c.drawImage(LOGO, x0, y - 24 * mm, width=lw, height=lw * LOGO_RATIO, mask=None)
        xt = x0 + lw + 5 * mm
    c.setFillColor(HexColor(TINTA))
    c.setFont("DVB", 18)
    c.drawString(xt, y - 2 * mm, d["negocio"])
    c.setFont("DV", 9.5)
    c.setFillColor(HexColor(GRIS))
    yy = y - 8 * mm
    for ln in d["lineas_neg"]:
        c.drawString(xt, yy, ln)
        yy -= 4.6 * mm
    if HAY_LOGO:
        yy = min(yy, y - 26 * mm)
    c.setFillColor(HexColor(TINTA))
    c.setFont("DVB", 12)
    c.drawRightString(x1, y, titulo)
    c.setFont("DVB", 16)
    c.setFillColor(HexColor(ACENTO))
    c.drawRightString(x1, y - 8 * mm, d["numero"])
    c.setFont("DV", 9.5)
    c.setFillColor(HexColor(GRIS))
    c.drawRightString(x1, y - 14 * mm, d["fecha"])
    return yy


def pdf(venta, negocio):
    d = _datos(venta, negocio)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setTitle(f"{d['titulo'].capitalize()} {d['numero']}")
    W, H = letter
    x0, x1 = 20 * mm, W - 20 * mm
    y = H - 22 * mm

    yy = _encabezado_pdf(c, d, d["titulo"], x0, x1, y)

    y = min(yy, y - 20 * mm) - 6 * mm
    # Datos de la venta
    c.setStrokeColor(HexColor(LINEA))
    alto = 20 * mm if d["doc"] else 16 * mm
    c.roundRect(x0, y - alto, x1 - x0, alto, 3 * mm, stroke=1, fill=0)
    cols = [("Cliente", d["cliente"]), ("Forma de pago", d["pago"]), ("Atendió", d["vendedor"])]
    cw = (x1 - x0) / 3
    for i, (k, v) in enumerate(cols):
        cx = x0 + 5 * mm + i * cw
        c.setFont("DV", 8)
        c.setFillColor(HexColor(GRIS))
        c.drawString(cx, y - 6 * mm, k.upper())
        c.setFont("DVB", 10.5)
        c.setFillColor(HexColor(TINTA))
        c.drawString(cx, y - 11.5 * mm, v[:34])
    if d["doc"]:
        c.setFont("DV", 9)
        c.setFillColor(HexColor(GRIS))
        c.drawString(x0 + 5 * mm, y - 16.5 * mm, f"C.C./NIT {d['doc']}"[:40])
    y -= alto + 10 * mm

    # Tabla
    cx_cant, cx_precio, cx_sub = x1 - 90 * mm, x1 - 40 * mm, x1
    c.setFont("DVB", 8.5)
    c.setFillColor(HexColor(GRIS))
    c.drawString(x0, y, "REFERENCIA / TALLA")
    c.drawRightString(cx_cant, y, "CANT.")
    c.drawRightString(cx_precio, y, "PRECIO UNIT.")
    c.drawRightString(cx_sub, y, "SUBTOTAL")
    y -= 3 * mm
    c.setStrokeColor(HexColor(TINTA))
    c.line(x0, y, x1, y)
    y -= 7 * mm
    c.setStrokeColor(HexColor(LINEA))
    for it in d["items"]:
        if y < 50 * mm:
            c.showPage()
            y = H - 25 * mm
        c.setFont("DV", 10.5)
        c.setFillColor(HexColor(TINTA))
        c.drawString(x0, y, it["nombre"][:52])
        c.drawRightString(cx_cant, y, str(it["cant"]))
        c.drawRightString(cx_precio, y, cop(it["precio"]))
        c.setFont("DVB", 10.5)
        c.drawRightString(cx_sub, y, cop(it["subtotal"]))
        y -= 4 * mm
        c.line(x0, y, x1, y)
        y -= 6 * mm

    # Totales
    if len(d["totales"]) > 1:
        for k, v in d["totales"]:
            c.setFont("DV", 10)
            c.setFillColor(HexColor(GRIS))
            c.drawString(x1 - 80 * mm, y, k)
            c.setFillColor(HexColor(TINTA))
            c.drawRightString(x1 - 5 * mm, y, cop(v))
            y -= 6 * mm
    y -= 2 * mm
    c.setFillColor(HexColor(ACENTO_SOFT))
    c.roundRect(x1 - 85 * mm, y - 14 * mm, 85 * mm, 16 * mm, 3 * mm, stroke=0, fill=1)
    c.setFillColor(HexColor(GRIS))
    c.setFont("DV", 9.5)
    c.drawString(x1 - 80 * mm, y - 4 * mm, f"{d['unidades']} par{'es' if d['unidades'] != 1 else ''}")
    c.setFillColor(HexColor(TINTA))
    c.setFont("DVB", 11)
    c.drawString(x1 - 80 * mm, y - 10 * mm, "TOTAL")
    c.setFont("DVB", 17)
    c.drawRightString(x1 - 5 * mm, y - 10 * mm, cop(d["total"]))
    y -= 22 * mm
    if d["saldo"] is not None:
        c.setFont("DV", 10)
        c.setFillColor(HexColor(GRIS))
        c.drawString(x1 - 80 * mm, y, "Abonado")
        c.drawRightString(x1 - 5 * mm, y, cop(d["abonado"]))
        y -= 6 * mm
        c.setFont("DVB", 11)
        c.setFillColor(HexColor(ROJO if d["saldo"] > 0.5 else TINTA))
        c.drawString(x1 - 80 * mm, y, "SALDO PENDIENTE")
        c.drawRightString(x1 - 5 * mm, y, cop(d["saldo"]))
        y -= 8 * mm
    y -= 4 * mm

    if d["nota"]:
        c.setFont("DV", 9.5)
        c.setFillColor(HexColor(GRIS))
        c.drawString(x0, y, f"Nota: {d['nota'][:100]}")
        y -= 8 * mm
    if d["mensaje"]:
        c.setFont("DVB", 11)
        c.setFillColor(HexColor(TINTA))
        c.drawCentredString(W / 2, y, d["mensaje"])

    c.setFont("DV", 8)
    c.setFillColor(HexColor(GRIS))
    c.drawCentredString(W / 2, 14 * mm, NOTA_LEGAL)

    if d["anulada"]:
        c.saveState()
        c.translate(W / 2, H / 2)
        c.rotate(30)
        c.setFillColor(HexColor(ROJO))
        c.setFillAlpha(0.18)
        c.setFont("DVB", 90)
        c.drawCentredString(0, 0, "ANULADA")
        c.restoreState()

    c.showPage()
    c.save()
    return buf.getvalue()


# --------------------------------------------------------------------- JPG
def jpg(venta, negocio):
    d = _datos(venta, negocio)
    W, M = 1080, 64
    f = lambda size, bold=False: ImageFont.truetype(F_BOLD if bold else F_REG, size)
    F = dict(neg=f(54, True), small=f(30), lbl=f(26), val=f(34, True), item=f(36),
             itemb=f(36, True), sub=f(30), tot=f(64, True), totl=f(36, True),
             num=f(46, True), msg=f(34, True), legal=f(24))

    def ancho(txt, font):
        return ImageDraw.Draw(Image.new("RGB", (1, 1))).textlength(txt, font=font)

    def cortar(txt, font, maxw):
        if ancho(txt, font) <= maxw:
            return [txt]
        palabras, lineas, actual = txt.split(), [], ""
        for p in palabras:
            prueba = (actual + " " + p).strip()
            if ancho(prueba, font) <= maxw:
                actual = prueba
            else:
                lineas.append(actual)
                actual = p
        lineas.append(actual)
        return lineas

    # Alto según el contenido
    alto_items = sum(46 * len(cortar(it["nombre"], F["item"], W - 2 * M - 300)) + 74 for it in d["items"])
    alto_items += 50 * (len(d["totales"]) if len(d["totales"]) > 1 else 0) + (110 if d["saldo"] is not None else 0)
    LOGO_W = 380
    H = 640 + (int(LOGO_W * LOGO_RATIO) + 30 if HAY_LOGO else 0) + 44 * len(d["lineas_neg"]) + alto_items + 280 + (60 if d["nota"] else 0) + (50 if d["doc"] else 0)
    img = Image.new("RGB", (W, H), "white")
    g = ImageDraw.Draw(img)

    g.rectangle([0, 0, W, 18], fill=ACENTO)
    y = 70
    if HAY_LOGO:
        lg = Image.open(LOGO).convert("RGB")
        lg = lg.resize((LOGO_W, LOGO_W * lg.height // lg.width), Image.LANCZOS)
        img.paste(lg, ((W - LOGO_W) // 2, 50))
        y = 50 + lg.height + 30
    for ln in cortar(d["negocio"], F["neg"], W - 2 * M):
        g.text((W / 2, y), ln, font=F["neg"], fill=TINTA, anchor="mt")
        y += 64
    for ln in d["lineas_neg"]:
        g.text((W / 2, y), ln, font=F["small"], fill=GRIS, anchor="mt")
        y += 44
    y += 24
    g.line([M, y, W - M, y], fill=LINEA, width=3)
    y += 34
    g.text((W / 2, y), d["titulo"], font=F["lbl"], fill=GRIS, anchor="mt")
    y += 42
    g.text((W / 2, y), d["numero"], font=F["num"], fill=ACENTO, anchor="mt")
    y += 64
    g.text((W / 2, y), d["fecha"], font=F["small"], fill=GRIS, anchor="mt")
    y += 64

    filas_d = [("Cliente", d["cliente"])] + ([("C.C./NIT", d["doc"])] if d["doc"] else []) + \
        [("Pago", d["pago"]), ("Atendió", d["vendedor"])]
    for k, v in filas_d:
        g.text((M, y), k, font=F["sub"], fill=GRIS)
        g.text((W - M, y), v[:32], font=F["val"], fill=TINTA, anchor="ra")
        y += 50
    y += 16
    g.line([M, y, W - M, y], fill=TINTA, width=3)
    y += 30

    for it in d["items"]:
        for ln in cortar(it["nombre"], F["item"], W - 2 * M - 300):
            g.text((M, y), ln, font=F["itemb"], fill=TINTA)
            y += 46
        g.text((M, y), f"{it['cant']} × {cop(it['precio'])}", font=F["sub"], fill=GRIS)
        g.text((W - M, y - 46), cop(it["subtotal"]), font=F["itemb"], fill=TINTA, anchor="ra")
        y += 44
        g.line([M, y, W - M, y], fill=LINEA, width=2)
        y += 30

    y += 10
    if len(d["totales"]) > 1:
        for k, v in d["totales"]:
            g.text((M + 36, y), k, font=F["sub"], fill=GRIS)
            g.text((W - M - 36, y), cop(v), font=F["val"], fill=TINTA, anchor="ra")
            y += 50
        y += 6
    g.rounded_rectangle([M, y, W - M, y + 150], radius=24, fill=ACENTO_SOFT)
    g.text((M + 36, y + 34), "TOTAL", font=F["totl"], fill=TINTA)
    g.text((M + 36, y + 86), f"{d['unidades']} par{'es' if d['unidades'] != 1 else ''}", font=F["sub"], fill=GRIS)
    g.text((W - M - 36, y + 75), cop(d["total"]), font=F["tot"], fill=TINTA, anchor="rm")
    y += 176
    if d["saldo"] is not None:
        g.text((M + 36, y), "Abonado", font=F["sub"], fill=GRIS)
        g.text((W - M - 36, y), cop(d["abonado"]), font=F["val"], fill=TINTA, anchor="ra")
        y += 50
        g.text((M + 36, y), "SALDO PENDIENTE", font=F["val"], fill=ROJO if d["saldo"] > 0.5 else TINTA)
        g.text((W - M - 36, y), cop(d["saldo"]), font=F["val"], fill=ROJO if d["saldo"] > 0.5 else TINTA, anchor="ra")
        y += 60
    y += 20

    if d["nota"]:
        g.text((W / 2, y), f"Nota: {d['nota'][:60]}", font=F["sub"], fill=GRIS, anchor="mt")
        y += 60
    if d["mensaje"]:
        g.text((W / 2, y), d["mensaje"], font=F["msg"], fill=TINTA, anchor="mt")
    g.text((W / 2, H - 50), NOTA_LEGAL, font=F["legal"], fill=GRIS, anchor="mt")

    if d["anulada"]:
        capa = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        t = Image.new("RGBA", (900, 220), (0, 0, 0, 0))
        ImageDraw.Draw(t).text((450, 110), "ANULADA", font=f(170, True), fill=(197, 48, 48, 95), anchor="mm")
        t = t.rotate(30, expand=True)
        capa.paste(t, ((W - t.width) // 2, (H - t.height) // 2), t)
        img = Image.alpha_composite(img.convert("RGBA"), capa).convert("RGB")

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


# ------------------------------------------------------- Documento de egreso
def numero_egreso(a):
    return f"EG-{a.id:06d}"


def egreso_pdf(a, negocio):
    """Documento de egreso por ajuste de inventario (pérdida, conteo físico, daño...)."""
    lineas_neg = _lineas_negocio(negocio)
    d = dict(negocio=negocio.nombre or "Mi negocio", lineas_neg=lineas_neg, numero=numero_egreso(a),
             fecha=a.fecha.strftime("%d/%m/%Y"))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setTitle(f"Egreso {d['numero']}")
    W, H = letter
    x0, x1 = 20 * mm, W - 20 * mm
    y = H - 22 * mm
    yy = _encabezado_pdf(c, d, "EGRESO DE INVENTARIO", x0, x1, y)
    y = min(yy, y - 20 * mm) - 6 * mm

    alto = 22 * mm if a.descripcion else 16 * mm
    c.setStrokeColor(HexColor(LINEA))
    c.roundRect(x0, y - alto, x1 - x0, alto, 3 * mm, stroke=1, fill=0)
    cols = [("Motivo", a.motivo), ("Registró", a.usuario.nombre if a.usuario else ""),
            ("Estado", "ANULADO" if a.anulada else "Vigente")]
    cw = (x1 - x0) / 3
    for i, (k, v) in enumerate(cols):
        cx = x0 + 5 * mm + i * cw
        c.setFont("DV", 8)
        c.setFillColor(HexColor(GRIS))
        c.drawString(cx, y - 6 * mm, k.upper())
        c.setFont("DVB", 10.5)
        c.setFillColor(HexColor(ROJO if v == "ANULADO" else TINTA))
        c.drawString(cx, y - 11.5 * mm, (v or "")[:34])
    if a.descripcion:
        c.setFont("DV", 9.5)
        c.setFillColor(HexColor(GRIS))
        c.drawString(x0 + 5 * mm, y - 18 * mm, f"Descripción: {a.descripcion}"[:110])
    y -= alto + 10 * mm

    cx_ant, cx_cant, cx_cu, cx_tot = x1 - 98 * mm, x1 - 68 * mm, x1 - 34 * mm, x1
    c.setFont("DVB", 8.5)
    c.setFillColor(HexColor(GRIS))
    c.drawString(x0, y, "MATERIAL / REFERENCIA")
    c.drawRightString(cx_ant, y, "EN SISTEMA")
    c.drawRightString(cx_cant, y, "SALE")
    c.drawRightString(cx_cu, y, "COSTO UNIT.")
    c.drawRightString(cx_tot, y, "COSTO TOTAL")
    y -= 3 * mm
    c.setStrokeColor(HexColor(TINTA))
    c.line(x0, y, x1, y)
    y -= 7 * mm
    c.setStrokeColor(HexColor(LINEA))

    def q(v):
        return f"{v:,.3f}".rstrip("0").rstrip(".").replace(",", "X").replace(".", ",").replace("X", ".")

    for it in a.items:
        if y < 60 * mm:
            c.showPage()
            y = H - 25 * mm
        u = it.unidad if it.insumo else "par"
        c.setFont("DV", 10.5)
        c.setFillColor(HexColor(TINTA))
        c.drawString(x0, y, it.nombre[:34])
        c.setFillColor(HexColor(GRIS))
        c.drawRightString(cx_ant, y, f"{q(it.stock_antes or 0)} {u}" if it.stock_antes is not None else "—")
        c.setFillColor(HexColor(TINTA))
        c.drawRightString(cx_cant, y, f"{q(it.cantidad)} {u}")
        c.drawRightString(cx_cu, y, cop(it.costo_unitario))
        c.setFont("DVB", 10.5)
        c.drawRightString(cx_tot, y, cop(it.costo_total))
        y -= 4 * mm
        c.line(x0, y, x1, y)
        y -= 6 * mm

    y -= 2 * mm
    c.setFillColor(HexColor("#fdf0f0"))
    c.roundRect(x1 - 85 * mm, y - 14 * mm, 85 * mm, 16 * mm, 3 * mm, stroke=0, fill=1)
    c.setFillColor(HexColor(GRIS))
    c.setFont("DV", 9.5)
    c.drawString(x1 - 80 * mm, y - 4 * mm, f"{len(a.items)} línea{'s' if len(a.items) != 1 else ''}")
    c.setFillColor(HexColor(TINTA))
    c.setFont("DVB", 11)
    c.drawString(x1 - 80 * mm, y - 10 * mm, "VALOR DEL EGRESO")
    c.setFont("DVB", 15)
    c.drawRightString(x1 - 5 * mm, y - 10 * mm, cop(a.total))
    y -= 40 * mm

    # Firmas
    for i, rot in enumerate(("Elaboró", "Revisó / aprobó")):
        fx = x0 + i * ((x1 - x0) / 2 + 5 * mm)
        c.setStrokeColor(HexColor(TINTA))
        c.line(fx, y, fx + 70 * mm, y)
        c.setFont("DV", 9)
        c.setFillColor(HexColor(GRIS))
        c.drawString(fx, y - 5 * mm, rot)

    c.setFont("DV", 8)
    c.setFillColor(HexColor(GRIS))
    c.drawCentredString(W / 2, 14 * mm, "Documento interno de egreso de inventario. Valorizado al costo promedio.")
    if a.anulada:
        c.saveState()
        c.translate(W / 2, H / 2)
        c.rotate(30)
        c.setFillColor(HexColor(ROJO))
        c.setFillAlpha(0.18)
        c.setFont("DVB", 90)
        c.drawCentredString(0, 0, "ANULADO")
        c.restoreState()
    c.showPage()
    c.save()
    return buf.getvalue()


# ------------------------------------------------------ Desprendible de nómina
def nomina_pdf(n, negocio):
    """Desprendible de pago para el empleado."""
    d = dict(negocio=negocio.nombre or "Mi negocio", lineas_neg=_lineas_negocio(negocio), numero=n.numero,
             fecha=n.fecha_pago.strftime("%d/%m/%Y"))
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setTitle(f"Desprendible {n.numero}")
    W, H = letter
    x0, x1 = 20 * mm, W - 20 * mm
    y = H - 22 * mm
    yy = _encabezado_pdf(c, d, "DESPRENDIBLE DE PAGO", x0, x1, y)
    y = min(yy, y - 20 * mm) - 6 * mm
    e = n.empleado
    c.setStrokeColor(HexColor(LINEA))
    c.roundRect(x0, y - 16 * mm, x1 - x0, 16 * mm, 3 * mm, stroke=1, fill=0)
    cols = [("Empleado", e.nombre), ("Documento", e.documento or "—"),
            ("Periodo", f"{n.desde.strftime('%d/%m')}–{n.hasta.strftime('%d/%m/%y')} · {n.dias} días")]
    cw = (x1 - x0) / 3
    for i, (k, v) in enumerate(cols):
        cx = x0 + 5 * mm + i * cw
        c.setFont("DV", 8)
        c.setFillColor(HexColor(GRIS))
        c.drawString(cx, y - 6 * mm, k.upper())
        c.setFont("DVB", 10.5)
        c.setFillColor(HexColor(TINTA))
        c.drawString(cx, y - 11.5 * mm, v[:34])
    y -= 26 * mm

    def bloque(titulo, filas, x, ancho):
        yy = y
        c.setFont("DVB", 9)
        c.setFillColor(HexColor(GRIS))
        c.drawString(x, yy, titulo)
        yy -= 3 * mm
        c.setStrokeColor(HexColor(TINTA))
        c.line(x, yy, x + ancho, yy)
        yy -= 6 * mm
        for k, v in filas:
            c.setFont("DV", 10)
            c.setFillColor(HexColor(TINTA))
            c.drawString(x, yy, k)
            c.drawRightString(x + ancho, yy, cop(v))
            yy -= 6.5 * mm
        return yy

    ancho = (x1 - x0) / 2 - 6 * mm
    dev = [(k, v) for k, v in (("Salario básico", n.basico), ("Auxilio de transporte", n.aux_transporte),
                               ("Destajo (producción)", n.destajo), ("Horas extra / recargos", n.horas_extra),
                               ("Bonificaciones", n.bonificaciones)) if v]
    ded = [(k, v) for k, v in (("Salud 4%", n.deduccion_salud), ("Pensión 4%", n.deduccion_pension),
                               (n.deducciones_detalle or "Otras deducciones", n.otras_deducciones)) if v]
    y1 = bloque("DEVENGADO", dev or [("—", 0)], x0, ancho)
    y2 = bloque("DEDUCCIONES", ded or [("Sin deducciones", 0)], x0 + ancho + 12 * mm, ancho)
    y = min(y1, y2) - 4 * mm
    c.setFont("DVB", 10.5)
    c.setFillColor(HexColor(TINTA))
    c.drawString(x0, y, "Total devengado")
    c.drawRightString(x0 + ancho, y, cop(n.devengado))
    c.drawString(x0 + ancho + 12 * mm, y, "Total deducciones")
    c.drawRightString(x1, y, cop(n.deducciones))
    y -= 14 * mm
    c.setFillColor(HexColor(ACENTO_SOFT))
    c.roundRect(x1 - 95 * mm, y - 10 * mm, 95 * mm, 16 * mm, 3 * mm, stroke=0, fill=1)
    c.setFillColor(HexColor(TINTA))
    c.setFont("DVB", 11)
    c.drawString(x1 - 90 * mm, y - 4 * mm, "NETO A PAGAR")
    c.setFont("DVB", 17)
    c.drawRightString(x1 - 5 * mm, y - 4 * mm, cop(n.neto))
    y -= 20 * mm
    if n.lineas_destajo:
        c.setFont("DVB", 9)
        c.setFillColor(HexColor(GRIS))
        c.drawString(x0, y, "DETALLE DEL DESTAJO")
        y -= 6 * mm
        c.setFont("DV", 9)
        for l in n.lineas_destajo:
            if y < 50 * mm:
                c.showPage()
                y = H - 25 * mm
                c.setFont("DV", 9)
            p = l.produccion
            c.setFillColor(HexColor(TINTA))
            c.drawString(x0, y, f"{p.fecha.strftime('%d/%m')} · Lote #{p.id} · {p.producto.etiqueta[:40]} · {l.proceso}")
            c.setFillColor(HexColor(GRIS))
            c.drawRightString(x1 - 30 * mm, y, l.detalle)
            c.setFillColor(HexColor(TINTA))
            c.drawRightString(x1, y, cop(l.total))
            y -= 5.5 * mm
        y -= 6 * mm
    y = max(y - 14 * mm, 40 * mm)
    for i, rot in enumerate(("Firma empleador", "Recibí conforme (empleado)")):
        fx = x0 + i * ((x1 - x0) / 2 + 5 * mm)
        c.setStrokeColor(HexColor(TINTA))
        c.line(fx, y, fx + 70 * mm, y)
        c.setFont("DV", 9)
        c.setFillColor(HexColor(GRIS))
        c.drawString(fx, y - 5 * mm, rot)
    c.setFont("DV", 8)
    c.setFillColor(HexColor(GRIS))
    c.drawCentredString(W / 2, 14 * mm, f"Medio de pago: {n.metodo or '—'} · Documento interno de pago de nómina.")
    if n.anulada:
        c.saveState()
        c.translate(W / 2, H / 2)
        c.rotate(30)
        c.setFillColor(HexColor(ROJO))
        c.setFillAlpha(0.18)
        c.setFont("DVB", 90)
        c.drawCentredString(0, 0, "ANULADO")
        c.restoreState()
    c.showPage()
    c.save()
    return buf.getvalue()
