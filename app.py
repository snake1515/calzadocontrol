"""CalzadoControl — materiales, producción por tallas y procesos, nómina, ventas y cartera
para fábricas y talleres de calzado."""
import os
import secrets
from collections import defaultdict
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import quote

from dotenv import load_dotenv
from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, send_file, session, url_for)
from flask_login import (LoginManager, current_user, login_required,
                         login_user, logout_user)
from sqlalchemy import func

from models import (ESTADOS_EMPRESA, Empresa, EmpresaMixin, empresa_id_actual, set_empresa,
                    CATEGORIAS_GASTO, CATEGORIAS_INSUMO, LINEAS, METODOS_PAGO, MODOS_MO, MOTIVOS_AJUSTE,
                    PROCESOS, ROLES, TIPOS_CLIENTE, TIPOS_PAGO, UNIDADES, Abono, Activo, Ajuste, Cliente,
                    CompraInsumo, Configuracion, DatosNegocio, Devolucion, Empleado, Gasto, Insumo,
                    PagoNomina, Preparacion, PrepRecetaItem, ProcesoTarifa, Produccion, ProduccionMO,
                    Producto, RecetaItem, Usuario, Venta, VentaItem, ImportLote, ahora, db, venta_real)
import importador
import comprobantes
import demo
import threading
import unicodedata
import nomina as nom
import reportes as rpt
import clientes_calc as ccalc
from unidades import ABREV, convertir, unidad_valida
from activos_calc import activos_vigentes, depreciacion_dia, depreciacion_periodo
from services import (ErrorNegocio, anular_ajuste, anular_compra, anular_preparacion, anular_produccion,
                      anular_venta, devolver_parcial, registrar_abono, registrar_ajuste, registrar_compra,
                      registrar_preparacion, registrar_produccion, registrar_venta)

load_dotenv()

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY") or secrets.token_hex(32)
db_url = (os.getenv("DATABASE_URL") or "sqlite:///calzado.db").strip().strip('"').strip("'")
if "[YOUR-PASSWORD]" in db_url:
    raise SystemExit("ERROR DATABASE_URL: aún contiene [YOUR-PASSWORD]. Reemplázalo por la contraseña real de Supabase.")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 280}
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_SAMESITE"] = "Lax"
app.config["REMEMBER_COOKIE_DURATION"] = timedelta(days=30)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
if os.getenv("RENDER"):  # en Render todo va por HTTPS: las cookies solo viajan cifradas
    app.config["SESSION_COOKIE_SECURE"] = True
    app.config["REMEMBER_COOKIE_SECURE"] = True
app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024  # nada de archivos gigantes
if not os.getenv("SECRET_KEY"):
    print("AVISO: falta la variable SECRET_KEY; las sesiones se cerrarán en cada reinicio.", flush=True)
NOMBRE_NEGOCIO = os.getenv("NOMBRE_NEGOCIO", "CalzadoControl")
# WhatsApp de SoftGest para que los clientes activen su plan (solo números, con 57)
SOFTGEST_WA = "".join(ch for ch in os.getenv("SOFTGEST_WHATSAPP", "") if ch.isdigit())
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 60 * 60 * 24 * 30
STATIC_V = (os.getenv("RENDER_GIT_COMMIT") or secrets.token_hex(4))[:8]
try:
    from flask_compress import Compress
    Compress(app)
except ImportError:
    pass

db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Inicia sesión para continuar."
login_manager.login_message_category = "info"


@login_manager.user_loader
def cargar_usuario(uid):
    return db.session.get(Usuario, int(uid))


# ---------------------------------------------------------------- utilidades
def n(valor, defecto=0.0):
    """Convierte texto a número aceptando 1.500,5 / 1500.5 / 1500,5."""
    if valor is None:
        return defecto
    v = str(valor).strip().replace(" ", "").replace("$", "")
    if not v:
        return defecto
    if "," in v and "." in v:
        v = v.replace(".", "").replace(",", ".")
    elif "," in v:
        v = v.replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return defecto


def fecha_form(texto):
    """Fecha del formulario + hora actual (si es hoy) o mediodía."""
    try:
        d = datetime.strptime(texto, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return ahora()
    hoy = ahora()
    if d == hoy.date():
        return hoy
    return datetime.combine(d, datetime.min.time()).replace(hour=12)


def _fecha(txt, defecto=None):
    try:
        return datetime.strptime(txt or "", "%Y-%m-%d").date()
    except ValueError:
        return defecto


def periodo():
    """Rango de fechas de los filtros (por defecto, el mes actual)."""
    hoy = ahora().date()
    desde = _fecha(request.args.get("desde"), hoy.replace(day=1))
    hasta = _fecha(request.args.get("hasta"), hoy)
    if hasta < desde:
        desde, hasta = hasta, desde
    ini = datetime.combine(desde, datetime.min.time())
    fin = datetime.combine(hasta + timedelta(days=1), datetime.min.time())
    return ini, fin, desde.isoformat(), hasta.isoformat()


def gastos_por_dia(desde, hasta):
    """dict date -> gastos + nómina (sin destajo), entre fechas date (inclusive)."""
    out = defaultdict(float)
    for f, v in (db.session.query(Gasto.fecha, func.sum(Gasto.valor))
                 .filter(Gasto.fecha >= desde, Gasto.fecha <= hasta).group_by(Gasto.fecha).all()):
        out[f] += v or 0
    for f, v in (db.session.query(PagoNomina.fecha_pago, func.sum(PagoNomina.gasto))
                 .filter(PagoNomina.anulada.is_(False), PagoNomina.fecha_pago >= desde,
                         PagoNomina.fecha_pago <= hasta).group_by(PagoNomina.fecha_pago).all()):
        out[f] += v or 0
    return out


def perdidas_por_dia(desde, hasta):
    """dict date -> valor de los egresos por ajuste (pérdidas), entre fechas date (inclusive)."""
    ini = datetime.combine(desde, datetime.min.time())
    fin = datetime.combine(hasta + timedelta(days=1), datetime.min.time())
    out = defaultdict(float)
    for f, v in (db.session.query(Ajuste.fecha, Ajuste.total)
                 .filter(Ajuste.anulada.is_(False), Ajuste.fecha >= ini, Ajuste.fecha < fin)):
        out[f.date()] += v or 0
    return out


def rol_requerido(*roles):
    def deco(f):
        @wraps(f)
        @login_required
        def envoltura(*a, **kw):
            if current_user.rol_efectivo not in roles:
                flash("No tienes permiso para entrar a esa sección.", "error")
                return redirect(url_for("inicio"))
            return f(*a, **kw)
        return envoltura
    return deco


def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


@app.before_request
def verificar_csrf():
    if request.method == "POST":
        enviado, esperado = request.form.get("csrf_token") or "", session.get("_csrf") or ""
        if not enviado or not esperado or not secrets.compare_digest(enviado, esperado):
            flash("La sesión expiró. Intenta de nuevo.", "error")
            return redirect(request.path)


@app.after_request
def cabeceras_seguridad(resp):
    """Cabeceras que impiden incrustar la app en otras páginas y otros abusos del navegador."""
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if app.config.get("SESSION_COOKIE_SECURE"):
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if current_user.is_authenticated and resp.mimetype == "text/html":
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


# Rutas que se pueden usar sin empresa activa o con la empresa bloqueada
LIBRES = {"static", "login", "logout", "cuenta", "bloqueado", "logo"}


@app.before_request
def contexto_empresa():
    """Define la empresa activa de esta petición: la del usuario, o la que SoftGest está atendiendo."""
    set_empresa(None)
    g.empresa = None
    g.soporte = False
    if not current_user.is_authenticated:
        return None
    ep = request.endpoint or ""
    if current_user.rol == "superadmin":
        emp = db.session.get(Empresa, session["soporte_empresa"]) if session.get("soporte_empresa") else None
        if emp:
            set_empresa(emp.id)
            g.empresa, g.soporte = emp, True
        elif not ep.startswith("sg_") and ep not in LIBRES:
            return redirect(url_for("sg_panel"))
        return None
    emp = current_user.empresa
    if emp is None:
        logout_user()
        return redirect(url_for("login"))
    set_empresa(emp.id)
    g.empresa = emp
    if emp.bloqueada and ep not in LIBRES:
        return redirect(url_for("bloqueado"))
    if ep.startswith("sg_"):
        abort(404)
    return None


_cache_neg = {}


def negocio_cache(refrescar=False):
    """Nombre y prefijo del negocio de la empresa activa, con caché de 10 minutos."""
    import time
    eid = empresa_id_actual()
    if eid is None:
        return dict(nombre=NOMBRE_NEGOCIO, prefijo="CZ", logo_v=0)
    c = _cache_neg.get(eid)
    if refrescar or c is None or time.time() > c[1]:
        val = dict(nombre=NOMBRE_NEGOCIO, prefijo="CZ", logo_v=0)
        try:
            neg = DatosNegocio.query.first()
            if neg:
                val = dict(nombre=neg.nombre or NOMBRE_NEGOCIO, prefijo=(neg.prefijo or "CZ").upper(),
                           logo_v=neg.logo_v if neg.logo else 0)
        except Exception:
            db.session.rollback()
        c = _cache_neg[eid] = (val, time.time() + 600)
    return c[0]


def num_doc(v):
    return f"{negocio_cache()['prefijo']}-{(v.consecutivo or v.id):06d}"


@app.context_processor
def contexto():
    def puede(*roles):
        return current_user.is_authenticated and current_user.rol_efectivo in roles
    neg = negocio_cache()
    emp = getattr(g, "empresa", None)
    return dict(csrf_token=csrf_token, puede=puede, ROLES=ROLES, STATIC_V=STATIC_V,
                NOMBRE_NEGOCIO=neg["nombre"], LOGO_V=neg["logo_v"], hoy=ahora().date().isoformat(), num_doc=num_doc,
                EMPRESA=emp, SOPORTE=getattr(g, "soporte", False), aviso_plan=aviso_plan(emp),
                wa_activar=wa_activar(emp))


def aviso_plan(emp):
    """Barra superior con los días que le quedan a la demo (o a la suscripción si está por vencer)."""
    if not emp or emp.vence is None:
        return None
    d = emp.dias_restantes
    if emp.estado == "activa" and d > 7:
        return None
    nivel = "err" if d <= 2 else ("warn" if d <= 5 else "info")
    if d < 0:
        txt = "Tu acceso venció."
    elif d == 0:
        txt = "Hoy es el último día de tu " + ("demo" if emp.estado == "demo" else "plan") + "."
    else:
        txt = (f"Demo gratis: te quedan <b>{d} día{'s' if d != 1 else ''}</b>" if emp.estado == "demo"
               else f"Tu plan vence en <b>{d} día{'s' if d != 1 else ''}</b>") + f" (hasta el {emp.vence.strftime('%d/%m/%Y')})."
    total = 15 if emp.estado == "demo" else 30
    return dict(texto=txt, nivel=nivel, dias=d, pct=max(min(d / total * 100, 100), 0))


def wa_activar(emp):
    if not emp or not SOFTGEST_WA:
        return None
    msg = (f"Hola SoftGest, soy de {emp.nombre}. Quiero activar CalzadoControl"
           + (" (estoy en la demo)." if emp.estado == "demo" else "."))
    return f"https://wa.me/{SOFTGEST_WA}?text={quote(msg)}"


@app.template_filter("cop")
def filtro_cop(v):
    v = v or 0
    s = f"{abs(v):,.0f}".replace(",", ".")
    return ("-$ " if v < -0.5 else "$ ") + s


@app.template_filter("num")
def filtro_num(v, dec=2):
    v = v or 0
    s = f"{v:,.{dec}f}"
    entero, _, decimal = s.partition(".")
    entero = entero.replace(",", ".")
    decimal = decimal.rstrip("0")
    return f"{entero},{decimal}" if decimal else entero


@app.template_filter("fecha")
def filtro_fecha(v, hora=False):
    if not v:
        return ""
    return v.strftime("%d/%m/%Y %I:%M %p" if hora else "%d/%m/%Y")


def leer_lineas(ids, cants, unidades, divisor=1.0):
    """Filas de receta/consumo del formulario → {Insumo: (cantidad en su unidad, unidad escrita)}."""
    out = {}
    unidades = list(unidades) + [""] * (len(ids) - len(unidades))
    for iid, c, u in zip(ids, cants, unidades):
        if not iid or n(c) <= 0:
            continue
        ins = db.session.get(Insumo, int(iid))
        if not ins:
            continue
        u = unidad_valida(u, ins.unidad)
        base = convertir(n(c), u, ins.unidad) / (divisor or 1)
        prev = out.get(ins, (0, u))[0]
        out[ins] = (prev + base, u)
    return out


def receta_json(items, rinde=1.0, attr="insumo"):
    """Receta guardada → filas para el formulario, en la unidad y la tanda en que se escribió."""
    filas = []
    for r in items:
        ins = getattr(r, attr)
        u = unidad_valida(r.unidad, ins.unidad)
        filas.append(dict(insumo_id=ins.id, unidad=u, base=r.cantidad,
                          cantidad=round(convertir(r.cantidad * (rinde or 1), ins.unidad, u), 6)))
    return filas


@app.template_filter("cant_receta")
def filtro_cant_receta(r, attr="insumo", rinde=1.0):
    """Muestra la cantidad de una línea de receta en su unidad escrita (ej. '15 dm²')."""
    ins = getattr(r, attr)
    u = unidad_valida(r.unidad, ins.unidad)
    return f"{filtro_num(convertir(r.cantidad * (rinde or 1), ins.unidad, u), 4)} {ABREV.get(u, u)}"


def insumos_json(solo_comprables=False):
    q = Insumo.query.filter_by(activo=True)
    if solo_comprables:
        q = q.filter(Insumo.es_preparacion.is_(False))
    return [dict(id=i.id, nombre=i.nombre, unidad=i.unidad, prep=i.es_preparacion, cat=i.categoria or "Otros",
                 costo=i.costo_promedio or 0, stock=i.stock or 0)
            for i in q.order_by(Insumo.categoria, Insumo.nombre)]


def empleados_json():
    return [dict(id=e.id, nombre=e.nombre, proceso=e.proceso or "", tipo=e.tipo_pago)
            for e in Empleado.query.filter_by(activo=True).order_by(Empleado.nombre)]


def leer_corrida(f, prefijo="corrida"):
    """{talla: pares} desde los campos corrida_talla[] / corrida_cant[]."""
    out = {}
    for t, c in zip(f.getlist(f"{prefijo}_talla[]"), f.getlist(f"{prefijo}_cant[]")):
        if str(t).strip() and int(n(c)) > 0:
            out[int(n(t))] = out.get(int(n(t)), 0) + int(n(c))
    return out


# ------------------------------------------------------------------- sesión
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("inicio"))
    if request.method == "POST":
        u = Usuario.query.filter(
            func.lower(Usuario.username) == request.form.get("username", "").strip().lower()
        ).first()
        momento = ahora()
        if u and u.bloqueado_hasta and u.bloqueado_hasta > momento:
            minutos = max(int((u.bloqueado_hasta - momento).total_seconds() // 60) + 1, 1)
            flash(f"Demasiados intentos fallidos. Intenta de nuevo en {minutos} minuto(s).", "error")
            return render_template("login.html")
        if u and u.activo and u.check_password(request.form.get("password", "")):
            u.intentos_fallidos, u.bloqueado_hasta = 0, None
            db.session.commit()
            session.clear()  # sesión nueva al entrar (evita fijación de sesión)
            login_user(u, remember=bool(request.form.get("recordar")))
            destino = request.args.get("next") or ""
            if not destino.startswith("/") or destino.startswith("//") or "\\" in destino:
                destino = url_for("inicio")  # solo se redirige dentro de la propia app
            return redirect(destino)
        if u:
            u.intentos_fallidos = (u.intentos_fallidos or 0) + 1
            if u.intentos_fallidos >= 5:
                u.bloqueado_hasta = momento + timedelta(minutes=15)
                u.intentos_fallidos = 0
            db.session.commit()
        import time
        time.sleep(0.6)  # frena los ataques de adivinar claves
        flash("Usuario o contraseña incorrectos.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))


@app.route("/cuenta", methods=["GET", "POST"])
@login_required
def cuenta():
    if request.method == "POST":
        actual = request.form.get("actual", "")
        nueva = request.form.get("nueva", "")
        if not current_user.check_password(actual):
            flash("La contraseña actual no es correcta.", "error")
        elif len(nueva) < 6:
            flash("La nueva contraseña debe tener al menos 6 caracteres.", "error")
        elif nueva != request.form.get("confirmar", ""):
            flash("Las contraseñas no coinciden.", "error")
        else:
            current_user.set_password(nueva)
            db.session.commit()
            flash("Contraseña actualizada.", "ok")
            return redirect(url_for("inicio"))
    return render_template("cuenta.html")


# ---------------------------------------------------------------- resumen
@app.route("/")
@login_required
def inicio():
    if current_user.rol_efectivo == "produccion":
        return redirect(url_for("producciones"))
    if current_user.rol_efectivo == "vendedor":
        return redirect(url_for("venta_nueva"))

    ini, fin, desde, hasta = periodo()
    ventas = Venta.query.filter(Venta.anulada.is_(False), venta_real(), Venta.fecha >= ini,
                                Venta.fecha < fin).all()
    ingresos = sum(v.total for v in ventas)
    costo_vendido = sum(v.costo_total for v in ventas)
    ganancia = ingresos - costo_vendido
    unidades_vendidas = sum(v.unidades for v in ventas)

    prods = Produccion.query.filter(Produccion.anulada.is_(False), Produccion.es_inicial.is_(False),
                                    Produccion.fecha >= ini,
                                    Produccion.fecha < fin).all()
    unidades_prod = sum(p.cantidad for p in prods)
    desglose = {"Materiales": sum(p.costo_insumos for p in prods)}
    mo_proc = defaultdict(float)
    for p in prods:
        for m in p.mano_obra:
            mo_proc[m.proceso] += m.total
    for proc in PROCESOS:
        if mo_proc.get(proc):
            desglose[f"M. obra · {proc}"] = mo_proc[proc]
    desglose.update({
        "Energía eléctrica": sum(p.costo_energia for p in prods),
        "Agua / gas": sum(p.costo_gas_agua for p in prods),
        "Transporte / maquila": sum(p.costo_transporte for p in prods),
        "Otros": sum(p.costo_otros for p in prods),
    })
    desglose = {k: v for k, v in desglose.items() if v or k == "Materiales"}
    costo_prod = sum(desglose.values())
    compras = db.session.query(func.coalesce(func.sum(CompraInsumo.costo_total), 0)).filter(
        CompraInsumo.anulada.is_(False), CompraInsumo.fecha >= ini, CompraInsumo.fecha < fin).scalar()
    compras_equipos = db.session.query(func.coalesce(func.sum(Activo.valor), 0)).filter(
        Activo.eliminado.is_(False), Activo.fecha_compra >= ini.date(),
        Activo.fecha_compra < fin.date()).scalar() or 0

    ranking = (db.session.query(
        VentaItem.producto_id, func.sum(VentaItem.cantidad), func.sum(VentaItem.subtotal),
        func.sum(VentaItem.subtotal - VentaItem.costo))
        .join(Venta, VentaItem.venta_id == Venta.id)
        .filter(Venta.anulada.is_(False), Venta.fecha >= ini, Venta.fecha < fin)
        .group_by(VentaItem.producto_id)
        .order_by(func.sum(VentaItem.cantidad).desc()).limit(10).all())
    pmap = {p.id: p for p in Producto.query.filter(Producto.id.in_([r[0] for r in ranking]))} if ranking else {}
    ranking = [dict(id=r[0], nombre=pmap[r[0]].etiqueta if r[0] in pmap else "—",
                    linea=pmap[r[0]].linea if r[0] in pmap else "", unidades=int(r[1] or 0),
                    ingresos=r[2] or 0, ganancia=r[3] or 0) for r in ranking]
    tallas_top = (db.session.query(VentaItem.talla, func.sum(VentaItem.cantidad))
                  .join(Venta, VentaItem.venta_id == Venta.id)
                  .filter(Venta.anulada.is_(False), Venta.fecha >= ini, Venta.fecha < fin,
                          VentaItem.talla.isnot(None))
                  .group_by(VentaItem.talla).order_by(VentaItem.talla).all())

    por_dia = defaultdict(lambda: [0.0, 0.0])
    for v in ventas:
        k = v.fecha.date().isoformat()
        por_dia[k][0] += v.total
        por_dia[k][1] += v.ganancia
    dias, d = [], ini.date()
    while d < fin.date():
        dias.append(d)
        d += timedelta(days=1)
    serie = dict(
        labels=[x.strftime("%d/%m") for x in dias],
        ventas=[round(por_dia[x.isoformat()][0]) for x in dias],
        ganancia=[round(por_dia[x.isoformat()][1]) for x in dias],
    )

    hoy_d = ahora().date()
    hasta_dep = min((fin - timedelta(days=1)).date(), hoy_d)
    depreciacion = depreciacion_periodo(ini.date(), hasta_dep) if hasta_dep >= ini.date() else 0
    gastos_det = (db.session.query(Gasto.categoria, func.sum(Gasto.valor))
                  .filter(Gasto.fecha >= ini.date(), Gasto.fecha < fin.date())
                  .group_by(Gasto.categoria).order_by(func.sum(Gasto.valor).desc()).all())
    nomina_gasto = db.session.query(func.coalesce(func.sum(PagoNomina.gasto), 0)).filter(
        PagoNomina.anulada.is_(False), PagoNomina.fecha_pago >= ini.date(),
        PagoNomina.fecha_pago < fin.date()).scalar() or 0
    nomina_neto = db.session.query(func.coalesce(func.sum(PagoNomina.neto), 0)).filter(
        PagoNomina.anulada.is_(False), PagoNomina.fecha_pago >= ini.date(),
        PagoNomina.fecha_pago < fin.date()).scalar() or 0
    perdidas = sum(perdidas_por_dia(ini.date(), (fin - timedelta(days=1)).date()).values())
    gastos_neg = sum(v or 0 for _, v in gastos_det)
    gastos_tot = gastos_neg + perdidas + nomina_gasto
    ganancia_neta = ganancia - depreciacion - gastos_tot

    credito = [v for v in Venta.query.filter(Venta.anulada.is_(False), Venta.condicion == "credito").all()
               if v.saldo > 0.5]
    cartera = sum(v.saldo for v in credito)
    cartera_vencida = sum(v.saldo for v in credito if v.vencida)
    recaudo = (sum(v.total_factura for v in ventas if v.condicion != "credito")
               + (db.session.query(func.coalesce(func.sum(Abono.valor), 0))
                  .filter(Abono.anulado.is_(False), Abono.fecha >= ini, Abono.fecha < fin).scalar() or 0))

    insumos = Insumo.query.filter_by(activo=True).all()
    productos = Producto.query.filter_by(activo=True).order_by(Producto.nombre).all()
    return render_template(
        "dashboard.html", desde=desde, hasta=hasta, ingresos=ingresos,
        costo_vendido=costo_vendido, ganancia=ganancia,
        depreciacion=depreciacion, ganancia_neta=ganancia_neta,
        gastos_det=gastos_det, gastos_tot=gastos_tot, gastos_neg=gastos_neg, perdidas=perdidas,
        nomina_gasto=nomina_gasto, nomina_neto=nomina_neto,
        margen=(ganancia / ingresos * 100) if ingresos else 0,
        unidades_vendidas=unidades_vendidas, num_ventas=len(ventas),
        unidades_prod=unidades_prod, costo_prod=costo_prod, desglose=desglose,
        costo_unit_prod=(costo_prod / unidades_prod) if unidades_prod else 0,
        compras=compras or 0, compras_equipos=compras_equipos, ranking=ranking, serie=serie,
        tallas_top=tallas_top, max_talla=max([c for _, c in tallas_top] + [1]),
        cartera=cartera, cartera_vencida=cartera_vencida, n_credito=len(credito), recaudo=recaudo,
        valor_insumos=sum(i.valor_stock for i in insumos),
        valor_productos=sum(max(p.stock, 0) * p.costo_promedio for p in productos),
        alertas_insumos=[i for i in insumos if i.bajo_minimo],
        alertas_productos=[p for p in productos if p.bajo_minimo],
        productos=productos,
    )


# ------------------------------------------------------------ distribución
@app.route("/distribucion")
@rol_requerido("admin")
def distribucion():
    ini, fin, desde, hasta = periodo()
    cfg = Configuracion.actual()
    ventas = Venta.query.filter(Venta.anulada.is_(False), venta_real(), Venta.fecha >= ini,
                                Venta.fecha < fin).all()
    por_dia = defaultdict(lambda: {"ventas": 0.0, "costo": 0.0, "num": 0})
    for v in ventas:
        d = por_dia[v.fecha.date()]
        d["ventas"] += v.total
        d["costo"] += v.costo_total
        d["num"] += 1
    pct = dict(reinversion=cfg.pct_reinversion, dueno=cfg.pct_dueno,
               emergencia=cfg.pct_emergencia)
    activos = activos_vigentes()

    gastos_dia = gastos_por_dia(ini.date(), (fin - timedelta(days=1)).date())
    for k, v in perdidas_por_dia(ini.date(), (fin - timedelta(days=1)).date()).items():
        gastos_dia[k] = gastos_dia.get(k, 0) + v

    def repartir(ventas_, costo_, equipos_, gastos_=0.0):
        ganancia = ventas_ - costo_
        fijos = equipos_ + gastos_
        if cfg.base == "ventas":
            base = ventas_
        else:
            base = max(ganancia - fijos, 0)
        r = {k: base * p / 100 for k, p in pct.items()}
        r.update(ventas=ventas_, costo=costo_, ganancia=ganancia, base=base, equipos=equipos_,
                 gastos=gastos_, neta=ganancia - fijos)
        r["reponer"] = costo_ if cfg.base == "ganancia" else 0
        r["faltante"] = (max(costo_ + fijos - r["reinversion"], 0)
                         if cfg.base == "ventas" else max(fijos - max(ganancia, 0), 0))
        return r

    hoy_d = ahora().date()
    filas = []
    dia = min((fin - timedelta(days=1)).date(), hoy_d)
    while dia >= ini.date():
        d = por_dia.get(dia, {"ventas": 0.0, "costo": 0.0, "num": 0})
        eq = depreciacion_dia(dia, activos)
        gd = gastos_dia.get(dia, 0)
        if d["num"] or eq > 0.005 or gd:
            f = repartir(d["ventas"], d["costo"], eq, gd)
            f.update(fecha=dia, num=d["num"])
            filas.append(f)
        dia -= timedelta(days=1)
    tot = repartir(sum(f["ventas"] for f in filas), sum(f["costo"] for f in filas),
                   sum(f["equipos"] for f in filas), sum(f["gastos"] for f in filas))
    return render_template("distribucion.html", cfg=cfg, filas=filas, tot=tot,
                           desde=desde, hasta=hasta)


@app.route("/configuracion", methods=["GET", "POST"])
@rol_requerido("admin")
def configuracion():
    cfg = Configuracion.actual()
    if request.method == "POST":
        f = request.form
        if f.get("seccion") == "nomina":
            smmlv, aux, arl = n(f.get("smmlv")), n(f.get("aux_transporte")), n(f.get("nom_arl_pct"))
            if smmlv <= 0 or aux < 0 or not 0 <= arl <= 10:
                flash("Revisa los valores de nómina.", "error")
            else:
                cfg.smmlv, cfg.aux_transporte, cfg.nom_arl_pct = smmlv, aux, arl
                cfg.nom_aportes = bool(f.get("nom_aportes"))
                cfg.nom_prestaciones = bool(f.get("nom_prestaciones"))
                cfg.nom_salud_empleador = bool(f.get("nom_salud_empleador"))
                db.session.commit()
                flash("Parámetros de nómina guardados.", "ok")
                return redirect(url_for("configuracion"))
        else:
            r, d, e = (n(f.get(k)) for k in ("pct_reinversion", "pct_dueno", "pct_emergencia"))
            base = f.get("base", "ventas")
            if min(r, d, e) < 0:
                flash("Los porcentajes no pueden ser negativos.", "error")
            elif abs(r + d + e - 100) > 0.01:
                flash(f"Los porcentajes deben sumar 100%. Ahora suman {r + d + e:g}%.", "error")
            elif base not in ("ventas", "ganancia"):
                flash("Base de cálculo no válida.", "error")
            else:
                cfg.pct_reinversion, cfg.pct_dueno, cfg.pct_emergencia, cfg.base = r, d, e, base
                db.session.commit()
                flash("Regla de distribución guardada.", "ok")
                return redirect(url_for("distribucion"))
    return render_template("configuracion.html", cfg=cfg)


# ------------------------------------------------------------------ activos
@app.route("/activos")
@rol_requerido("admin")
def activos():
    hoy_d = ahora().date()
    items = Activo.query.filter_by(eliminado=False).order_by(Activo.fecha_baja.isnot(None),
                                                               Activo.fecha_compra.desc()).all()
    filas = []
    for a in items:
        acum = a.acumulada(hoy_d)
        meses_rest = 0
        if a.estado(hoy_d) == "vigente":
            meses_rest = max((a.fin_vida - hoy_d).days, 0) / (365 / 12)
        filas.append(dict(a=a, acum=acum, pendiente=a.depreciable - acum,
                          pct=(acum / a.depreciable * 100) if a.depreciable else 100,
                          estado=a.estado(hoy_d), meses_rest=meses_rest))
    vig = [f for f in filas if f["estado"] == "vigente"]
    inicio_mes = hoy_d.replace(day=1)
    return render_template(
        "activos.html", filas=filas, hoy=hoy_d.isoformat(),
        total_valor=sum(f["a"].valor for f in filas if f["estado"] != "baja"),
        total_pendiente=sum(f["pendiente"] for f in vig),
        cuota_mes=sum(f["a"].cuota_mensual for f in vig),
        dep_mes=depreciacion_periodo(inicio_mes, hoy_d, [f["a"] for f in filas]))


@app.route("/activos/nuevo", methods=["GET", "POST"])
@app.route("/activos/<int:id>/editar", methods=["GET", "POST"])
@rol_requerido("admin")
def activo_form(id=None):
    a = db.session.get(Activo, id) if id else None
    if id and (not a or a.eliminado):
        abort(404)
    if request.method == "POST":
        f = request.form
        nombre = f.get("nombre", "").strip()
        valor, residual = n(f.get("valor")), n(f.get("valor_residual"))
        vida = int(n(f.get("vida_util_meses")))
        fecha = _fecha(f.get("fecha_compra"))
        errores = []
        if not nombre:
            errores.append("Escribe el nombre de la máquina o herramienta.")
        if not fecha:
            errores.append("Indica la fecha de compra.")
        elif fecha > ahora().date():
            errores.append("La fecha de compra no puede ser futura.")
        if valor <= 0:
            errores.append("El valor de compra debe ser mayor a 0.")
        if residual < 0 or residual >= valor > 0:
            errores.append("El valor residual debe ser menor que el valor de compra.")
        if vida < 1:
            errores.append("La vida útil debe ser de al menos 1 mes.")
        if errores:
            for m in errores:
                flash(m, "error")
        else:
            if not a:
                a = Activo()
                db.session.add(a)
            a.nombre, a.fecha_compra, a.valor = nombre[:120], fecha, valor
            a.valor_residual, a.vida_util_meses = residual, vida
            a.descripcion = f.get("descripcion", "").strip()[:255] or None
            a.proveedor = f.get("proveedor", "").strip()[:120] or None
            db.session.commit()
            flash(f"«{nombre}» guardado. Ya aparece en Compras y en Máquinas.", "ok")
            return redirect(url_for("compras") if f.get("origen") == "compras" else url_for("activos"))
    return render_template("activo_form.html", a=a, origen=request.values.get("origen", ""))


@app.route("/activos/<int:id>/baja", methods=["POST"])
@rol_requerido("admin")
def activo_baja(id):
    a = db.session.get(Activo, id) or abort(404)
    if request.form.get("deshacer"):
        a.fecha_baja, a.motivo_baja = None, None
        flash(f"«{a.nombre}» vuelve a estar en uso.", "ok")
    else:
        a.fecha_baja = _fecha(request.form.get("fecha_baja"), ahora().date())
        a.motivo_baja = request.form.get("motivo", "").strip()[:255] or None
        flash(f"«{a.nombre}» dado de baja. Deja de depreciarse desde esa fecha.", "ok")
    db.session.commit()
    return redirect(url_for("activos"))


@app.route("/activos/<int:id>/eliminar", methods=["POST"])
@rol_requerido("admin")
def activo_eliminar(id):
    a = db.session.get(Activo, id) or abort(404)
    a.eliminado = True
    db.session.commit()
    flash(f"«{a.nombre}» eliminado (registrado por error).", "ok")
    return redirect(url_for("activos"))


# ----------------------------------------------------------------- reportes
def _params_reporte():
    agrupar = request.args.get("agrupar", "semanal")
    if agrupar not in rpt.AGRUPACIONES:
        agrupar = "semanal"
    hoy_d = ahora().date()
    d0, h0 = rpt.rango_por_defecto(agrupar, hoy_d)
    desde = _fecha(request.args.get("desde"), d0)
    hasta = _fecha(request.args.get("hasta"), h0)
    if hasta < desde:
        desde, hasta = hasta, desde
    if (hasta - desde).days > 3 * 366:
        desde = hasta - timedelta(days=3 * 366)
    return agrupar, desde, hasta


@app.route("/reportes")
@rol_requerido("admin")
def reportes():
    agrupar, desde, hasta = _params_reporte()
    rep = rpt.generar(desde, hasta, agrupar)
    serie = dict(labels=[f["periodo"] for f in rep["filas"]],
                 ventas=[round(f["ventas"]) for f in rep["filas"]],
                 compras=[round(f["compras"] + f["compras_eq"]) for f in rep["filas"]],
                 neta=[round(f["neta"]) for f in rep["filas"]])
    return render_template("reportes.html", rep=rep, serie=serie, agrupar=agrupar,
                           agrupaciones=rpt.AGRUPACIONES, columnas=rpt.COLUMNAS,
                           desde=desde.isoformat(), hasta=hasta.isoformat())


@app.route("/reportes/excel")
@rol_requerido("admin")
def reportes_excel():
    agrupar, desde, hasta = _params_reporte()
    rep = rpt.generar(desde, hasta, agrupar)
    neg = DatosNegocio.actual()
    data = rpt.excel(rep, desde, hasta, agrupar, neg.nombre, neg.prefijo)
    nombre = f"reporte_{agrupar}_{desde.isoformat()}_a_{hasta.isoformat()}.xlsx"
    return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


# ----------------------------------------------------------------- clientes
@app.route("/clientes")
@rol_requerido("admin", "vendedor")
def clientes():
    hoy_d = ahora().date()
    q = request.args.get("q", "").strip()
    estado = request.args.get("estado", "")
    clis = Cliente.query.filter_by(activo=True).order_by(Cliente.nombre).all()
    met = ccalc.metricas(clis, hoy_d)
    filas = [dict(c=c, **met[c.id]) for c in clis
             if (not q or q.lower() in c.nombre.lower() or q in (c.telefono or "")
                 or q.lower() in (c.ciudad or "").lower())
             and (not estado or met[c.id]["estado"] == estado
                  or (estado == "debe" and met[c.id]["saldo"] > 0.5)
                  or (estado == "vencido" and met[c.id]["vencido"] > 0.5))]
    filas.sort(key=lambda f: (-f["v90"], -(f["total_v"])))
    todos = list(met.values())
    cuenta = {k: sum(1 for m in todos if m["estado"] == k) for k in ccalc.ESTADOS}
    cuenta["debe"] = sum(1 for m in todos if m["saldo"] > 0.5)
    cuenta["vencido"] = sum(1 for m in todos if m["vencido"] > 0.5)
    return render_template("clientes.html", filas=filas, q=q, estado=estado, cuenta=cuenta,
                           total=len(clis), estados=ccalc.ESTADOS)


@app.route("/clientes/nuevo", methods=["GET", "POST"])
@app.route("/clientes/<int:id>/editar", methods=["GET", "POST"])
@rol_requerido("admin", "vendedor")
def cliente_form(id=None):
    c = db.session.get(Cliente, id) if id else None
    if id and not c:
        abort(404)
    if request.method == "POST":
        f = request.form
        nombre = f.get("nombre", "").strip()
        dup = Cliente.query.filter(func.lower(Cliente.nombre) == nombre.lower(), Cliente.activo.is_(True),
                                   Cliente.id != (id or 0)).first()
        if not nombre:
            flash("Escribe el nombre del cliente.", "error")
        elif dup:
            flash("Ya existe un cliente con ese nombre.", "error")
        else:
            if not c:
                c = Cliente()
                db.session.add(c)
            c.nombre = nombre[:120]
            for campo, largo in (("telefono", 40), ("email", 120), ("direccion", 160), ("ciudad", 80),
                                 ("identificacion", 40), ("nota", 255)):
                setattr(c, campo, f.get(campo, "").strip()[:largo] or None)
            c.tipo = f.get("tipo") if f.get("tipo") in TIPOS_CLIENTE else "Otro"
            c.plazo_dias = max(int(n(f.get("plazo_dias"))), 0)
            cupo = n(f.get("cupo"))
            c.cupo = cupo if cupo > 0 else None
            c.usa_precio_mayor = bool(f.get("usa_precio_mayor"))
            db.session.commit()
            flash(f"Cliente «{c.nombre}» guardado.", "ok")
            return redirect(url_for("cliente_detalle", id=c.id))
    return render_template("cliente_form.html", c=c, tipos=TIPOS_CLIENTE)


@app.route("/clientes/<int:id>")
@rol_requerido("admin", "vendedor")
def cliente_detalle(id):
    c = db.session.get(Cliente, id) or abort(404)
    hoy_d = ahora().date()
    m = ccalc.metricas([c], hoy_d)[c.id]
    ventas_c = Venta.query.filter(Venta.cliente_id == id).order_by(Venta.fecha.desc()).limit(30).all()
    pendientes = [v for v in Venta.query.filter(Venta.cliente_id == id, Venta.anulada.is_(False),
                                                Venta.condicion == "credito").order_by(Venta.vence).all()
                  if v.saldo > 0.5]
    favoritos = (db.session.query(VentaItem.producto_id, func.sum(VentaItem.cantidad))
                 .join(Venta, VentaItem.venta_id == Venta.id)
                 .filter(Venta.cliente_id == id, Venta.anulada.is_(False))
                 .group_by(VentaItem.producto_id).order_by(func.sum(VentaItem.cantidad).desc()).limit(8).all())
    pm = {p.id: p for p in Producto.query.filter(Producto.id.in_([x[0] for x in favoritos]))} if favoritos else {}
    favoritos = [(pm[i].etiqueta if i in pm else "—", u) for i, u in favoritos]
    meses = [dict(etq=f"{rpt.MESES[mm - 1].capitalize()} {str(yy)[2:]}", u=u)
             for (yy, mm), u in ccalc.por_mes(id, hoy_d)]
    return render_template("cliente_detalle.html", c=c, m=m, ventas=ventas_c, favoritos=favoritos,
                           meses=meses, max_mes=max([x["u"] for x in meses] + [1]), pendientes=pendientes,
                           recordatorio=_texto_recordatorio(c, pendientes))


def _texto_recordatorio(c, pendientes):
    if not pendientes or not c.whatsapp:
        return None
    total = sum(v.saldo for v in pendientes)
    det = "; ".join(f"{num_doc(v)} por {filtro_cop(v.saldo)} (vence {v.vence.strftime('%d/%m')})"
                    for v in pendientes[:6])
    txt = (f"Hola {c.nombre}, le saluda {negocio_cache()['nombre']}. Le recordamos su saldo pendiente de "
           f"{filtro_cop(total)}: {det}. ¡Gracias!")
    return f"{c.whatsapp}?text={quote(txt)}"


@app.route("/clientes/<int:id>/eliminar", methods=["POST"])
@rol_requerido("admin")
def cliente_eliminar(id):
    c = db.session.get(Cliente, id) or abort(404)
    c.activo = False
    db.session.commit()
    flash(f"Cliente «{c.nombre}» retirado (sus ventas se conservan).", "ok")
    return redirect(url_for("clientes"))


# ------------------------------------------------------------------ cartera
@app.route("/cartera")
@rol_requerido("admin", "vendedor")
def cartera():
    hoy_d = ahora().date()
    filtro = request.args.get("f", "")
    ventas_c = [v for v in Venta.query.filter(Venta.anulada.is_(False), Venta.condicion == "credito")
                .order_by(Venta.vence, Venta.id).all() if v.saldo > 0.5]
    if current_user.rol_efectivo == "vendedor":
        ventas_c = [v for v in ventas_c if v.usuario_id == current_user.id]
    total = sum(v.saldo for v in ventas_c)
    vencidas = [v for v in ventas_c if v.vencida]
    semana = [v for v in ventas_c if not v.vencida and v.vence and v.vence <= hoy_d + timedelta(days=7)]
    if filtro == "vencidas":
        ventas_c = vencidas
    elif filtro == "semana":
        ventas_c = semana
    rangos = {"Al día": 0.0, "1–30 días": 0.0, "31–60 días": 0.0, "61–90 días": 0.0, "Más de 90": 0.0}
    for v in [x for x in Venta.query.filter(Venta.anulada.is_(False), Venta.condicion == "credito").all()
              if x.saldo > 0.5]:
        dv = (hoy_d - v.vence).days if v.vence else 0
        k = ("Al día" if dv <= 0 else "1–30 días" if dv <= 30 else "31–60 días" if dv <= 60
             else "61–90 días" if dv <= 90 else "Más de 90")
        rangos[k] += v.saldo
    ini_mes = datetime.combine(hoy_d.replace(day=1), datetime.min.time())
    recaudo_mes = db.session.query(func.coalesce(func.sum(Abono.valor), 0)).filter(
        Abono.anulado.is_(False), Abono.fecha >= ini_mes).scalar() or 0
    return render_template("cartera.html", ventas=ventas_c, total=total, filtro=filtro,
                           total_vencido=sum(v.saldo for v in vencidas), n_vencidas=len(vencidas),
                           total_semana=sum(v.saldo for v in semana), n_semana=len(semana),
                           rangos=rangos, recaudo_mes=recaudo_mes, metodos=METODOS_PAGO)


@app.route("/ventas/<int:id>/abono", methods=["POST"])
@rol_requerido("admin", "vendedor")
def venta_abono(id):
    v = db.session.get(Venta, id) or abort(404)
    try:
        a = registrar_abono(v, n(request.form.get("valor")), request.form.get("metodo", "Efectivo"),
                            request.form.get("nota", ""), fecha_form(request.form.get("fecha")),
                            current_user.id)
        db.session.commit()
        msg = f"Abono de {filtro_cop(a.valor)} registrado. "
        flash(msg + ("La venta quedó pagada. ✔" if v.saldo <= 0.5 else f"Saldo: {filtro_cop(v.saldo)}."), "ok")
    except ErrorNegocio as e:
        db.session.rollback()
        for m in e.errores:
            flash(m, "error")
    return redirect(request.form.get("volver") or url_for("venta_detalle", id=id))


@app.route("/abonos/<int:id>/anular", methods=["POST"])
@rol_requerido("admin")
def abono_anular(id):
    a = db.session.get(Abono, id) or abort(404)
    if a.anulado:
        flash("Ese abono ya estaba anulado.", "error")
    else:
        a.anulado = True
        db.session.commit()
        flash(f"Abono de {filtro_cop(a.valor)} anulado. El saldo de la venta volvió a subir.", "ok")
    return redirect(url_for("venta_detalle", id=a.venta_id))


# ------------------------------------------------------------------- gastos
@app.route("/gastos")
@rol_requerido("admin")
def gastos():
    ini, fin, desde, hasta = periodo()
    cat = request.args.get("categoria", "")
    q = Gasto.query.filter(Gasto.fecha >= ini.date(), Gasto.fecha < fin.date())
    if cat:
        q = q.filter(Gasto.categoria == cat)
    items = q.order_by(Gasto.fecha.desc(), Gasto.id.desc()).all()
    por_cat = defaultdict(float)
    for g in items:
        por_cat[g.categoria] += g.valor
    por_cat = sorted(por_cat.items(), key=lambda x: -x[1])
    nomina_gasto = db.session.query(func.coalesce(func.sum(PagoNomina.gasto), 0)).filter(
        PagoNomina.anulada.is_(False), PagoNomina.fecha_pago >= ini.date(),
        PagoNomina.fecha_pago < fin.date()).scalar() or 0
    return render_template("gastos.html", items=items, desde=desde, hasta=hasta, cat=cat,
                           categorias=CATEGORIAS_GASTO, por_cat=por_cat, nomina_gasto=nomina_gasto,
                           total=sum(g.valor for g in items))


@app.route("/gastos/nuevo", methods=["GET", "POST"])
@app.route("/gastos/<int:id>/editar", methods=["GET", "POST"])
@rol_requerido("admin")
def gasto_form(id=None):
    g = db.session.get(Gasto, id) if id else None
    if id and not g:
        abort(404)
    if request.method == "POST":
        f = request.form
        valor = n(f.get("valor"))
        fecha = _fecha(f.get("fecha"))
        cat = f.get("categoria", "Otros")
        desc = f.get("descripcion", "").strip()
        if valor <= 0:
            flash("El valor debe ser mayor a 0.", "error")
        elif not fecha:
            flash("Indica la fecha del gasto.", "error")
        elif cat not in CATEGORIAS_GASTO:
            flash("Categoría no válida.", "error")
        elif cat == "Otros" and not desc:
            flash("Describe en qué consiste el gasto «Otros».", "error")
        else:
            if not g:
                g = Gasto(usuario_id=current_user.id)
                db.session.add(g)
            g.fecha, g.categoria, g.valor, g.descripcion = fecha, cat, valor, desc[:255] or None
            db.session.commit()
            flash(f"Gasto de {filtro_cop(valor)} en {cat} guardado.", "ok")
            return redirect(url_for("gastos"))
    return render_template("gasto_form.html", g=g, categorias=CATEGORIAS_GASTO,
                           pre_cat=request.args.get("categoria", ""))


@app.route("/gastos/<int:id>/eliminar", methods=["POST"])
@rol_requerido("admin")
def gasto_eliminar(id):
    g = db.session.get(Gasto, id) or abort(404)
    db.session.delete(g)
    db.session.commit()
    flash("Gasto eliminado.", "ok")
    return redirect(url_for("gastos"))


# ---------------------------------------------------------------- insumos
@app.route("/insumos")
@rol_requerido("admin", "produccion")
def insumos():
    q = request.args.get("q", "").strip()
    cat = request.args.get("cat", "")
    query = Insumo.query.filter_by(activo=True)
    if q:
        query = query.filter(Insumo.nombre.ilike(f"%{q}%"))
    if cat:
        query = query.filter(Insumo.categoria == cat)
    items = query.order_by(Insumo.categoria, Insumo.nombre).all()
    return render_template("insumos.html", items=items, q=q, cat=cat, categorias=CATEGORIAS_INSUMO,
                           total=sum(i.valor_stock for i in items))


@app.route("/insumos/nuevo", methods=["GET", "POST"])
@app.route("/insumos/<int:id>/editar", methods=["GET", "POST"])
@rol_requerido("admin", "produccion")
def insumo_form(id=None):
    ins = db.session.get(Insumo, id) if id else None
    if id and not ins:
        abort(404)
    if request.method == "POST":
        f = request.form
        nombre = f.get("nombre", "").strip()
        unidad = f.get("unidad") if f.get("unidad") in UNIDADES else "unidad"
        categoria = f.get("categoria") if f.get("categoria") in CATEGORIAS_INSUMO else "Otros"
        minimo = n(f.get("stock_minimo"))
        es_prep = bool(f.get("es_preparacion"))
        rinde = n(f.get("tanda_rinde"), 1) or 1
        receta = leer_lineas(f.getlist("prep_insumo[]"), f.getlist("prep_cantidad[]"),
                             f.getlist("prep_unidad[]"), divisor=rinde) if es_prep else {}
        dup = Insumo.query.filter(func.lower(Insumo.nombre) == nombre.lower(),
                                  Insumo.activo.is_(True), Insumo.id != (id or 0)).first()
        if not nombre:
            flash("Escribe el nombre del material.", "error")
        elif dup:
            flash("Ya existe un material activo con ese nombre.", "error")
        elif es_prep and not receta:
            flash("Agrega la composición del semielaborado: qué materiales lleva y cuánto por cada "
                  f"{unidad}.", "error")
        elif ins and ins in receta:
            flash("Un semielaborado no puede llevarse a sí mismo.", "error")
        else:
            try:
                nuevo = ins is None
                if nuevo:
                    ins = Insumo(nombre=nombre, unidad=unidad, stock_minimo=minimo)
                    db.session.add(ins)
                ins.nombre, ins.unidad, ins.stock_minimo, ins.es_preparacion = nombre, unidad, minimo, es_prep
                ins.categoria = categoria
                ins.tanda_rinde = rinde if es_prep else None
                ins.receta_prep.clear()
                db.session.flush()
                for comp, (c, u) in receta.items():
                    if not comp.es_preparacion:
                        ins.receta_prep.append(PrepRecetaItem(componente=comp, cantidad=c, unidad=u))
                if nuevo and not es_prep:
                    cant = n(f.get("cantidad_inicial"))
                    if cant > 0:
                        registrar_compra(ins, cant, n(f.get("costo_inicial")), nota="Inventario inicial",
                                         fecha=fecha_form(f.get("fecha_inicial")),
                                         usuario_id=current_user.id)
                db.session.commit()
                flash(f"{'Semielaborado' if es_prep else 'Material'} «{nombre}» guardado.", "ok")
                return redirect(url_for("insumos"))
            except ErrorNegocio as e:
                db.session.rollback()
                for m in e.errores:
                    flash(m, "error")
    receta = receta_json(ins.receta_prep, ins.tanda_rinde or 1, attr="componente") if ins else []
    comps = [i for i in insumos_json(solo_comprables=True) if not ins or i["id"] != ins.id]
    return render_template("insumo_form.html", ins=ins, unidades=UNIDADES, componentes=comps,
                           receta_prep=receta, categorias=CATEGORIAS_INSUMO)


@app.route("/insumos/<int:id>/eliminar", methods=["POST"])
@rol_requerido("admin")
def insumo_eliminar(id):
    ins = db.session.get(Insumo, id) or abort(404)
    if RecetaItem.query.filter_by(insumo_id=id).join(Producto).filter(Producto.activo.is_(True)).count():
        flash("Ese material está en la ficha de consumo de una referencia activa. Quítalo de ahí primero.", "error")
    elif PrepRecetaItem.query.filter_by(componente_id=id).join(
            Insumo, PrepRecetaItem.preparacion_id == Insumo.id).filter(Insumo.activo.is_(True)).count():
        flash("Ese material se usa en un semielaborado. Quítalo de esa composición primero.", "error")
    else:
        ins.activo = False
        db.session.commit()
        flash(f"Material «{ins.nombre}» eliminado (su historial se conserva).", "ok")
    return redirect(url_for("insumos"))


# ---------------------------------------------------------------- compras
@app.route("/compras")
@rol_requerido("admin", "produccion")
def compras():
    ini, fin, desde, hasta = periodo()
    tipo = request.args.get("tipo", "")
    filas = []
    if tipo in ("", "insumos"):
        for c in (CompraInsumo.query.filter(CompraInsumo.fecha >= ini, CompraInsumo.fecha < fin)
                  .order_by(CompraInsumo.fecha.desc()).all()):
            filas.append(dict(tipo="insumo", fecha=c.fecha, orden=c.fecha, nombre=c.insumo.nombre,
                              cantidad=f"{filtro_num(c.cantidad, 3)} {c.insumo.unidad}", unitario=c.costo_unitario,
                              total=c.costo_total, proveedor=c.proveedor, nota=c.nota,
                              anulada=c.anulada, obj=c))
    if tipo in ("", "equipos") and current_user.rol_efectivo == "admin":
        for a in (Activo.query.filter(Activo.eliminado.is_(False), Activo.fecha_compra >= ini.date(),
                                      Activo.fecha_compra < fin.date()).all()):
            filas.append(dict(tipo="equipo", fecha=a.fecha_compra,
                              orden=datetime.combine(a.fecha_compra, datetime.min.time()),
                              nombre=a.nombre, cantidad="1 máquina", unitario=a.valor, total=a.valor,
                              proveedor=a.proveedor, nota=a.descripcion, anulada=False, obj=a))
    filas.sort(key=lambda x: x["orden"], reverse=True)
    t_ins = sum(f["total"] for f in filas if f["tipo"] == "insumo" and not f["anulada"])
    t_eq = sum(f["total"] for f in filas if f["tipo"] == "equipo")
    return render_template("compras.html", filas=filas, desde=desde, hasta=hasta, tipo=tipo,
                           total=t_ins + t_eq, total_insumos=t_ins, total_equipos=t_eq)


@app.route("/compras/nueva", methods=["GET", "POST"])
@rol_requerido("admin", "produccion")
def compra_nueva():
    if request.method == "POST":
        ids = request.form.getlist("insumo_id[]")
        cants = request.form.getlist("cantidad[]")
        costos = request.form.getlist("costo[]")
        fecha = fecha_form(request.form.get("fecha"))
        proveedor = request.form.get("proveedor", "").strip()
        nota = request.form.get("nota", "").strip()
        registradas = 0
        try:
            for iid, c, ct in zip(ids, cants, costos):
                if not iid:
                    continue
                ins = db.session.get(Insumo, int(iid))
                if not ins:
                    continue
                registrar_compra(ins, n(c), n(ct), proveedor, nota, fecha, current_user.id)
                registradas += 1
            if not registradas:
                raise ErrorNegocio("Agrega al menos un material a la compra.")
            db.session.commit()
            flash(f"Compra registrada ({registradas} material(es)). El inventario y el costo promedio se actualizaron.", "ok")
            return redirect(url_for("compras"))
        except ErrorNegocio as e:
            db.session.rollback()
            for m in e.errores:
                flash(m, "error")
    return render_template("compra_form.html", insumos=insumos_json(solo_comprables=True))


@app.route("/compras/<int:id>/editar", methods=["GET", "POST"])
@rol_requerido("admin", "produccion")
def compra_editar(id):
    c = db.session.get(CompraInsumo, id) or abort(404)
    if c.anulada:
        flash("Esta compra está anulada; no se puede editar.", "error")
        return redirect(url_for("compras"))
    if request.method == "POST":
        nueva = _fecha(request.form.get("fecha"))
        if not nueva:
            flash("Indica una fecha válida.", "error")
        elif nueva > ahora().date():
            flash("La fecha no puede ser futura.", "error")
        else:
            if nueva != c.fecha.date():
                c.fecha = fecha_form(nueva.isoformat())
            c.proveedor = request.form.get("proveedor", "").strip()[:120] or None
            c.nota = request.form.get("nota", "").strip()[:255] or None
            db.session.commit()
            flash(f"Compra de «{c.insumo.nombre}» actualizada.", "ok")
            return redirect(url_for("compras", desde=min(nueva, ahora().date().replace(day=1)).isoformat()))
    return render_template("compra_editar.html", c=c)


@app.route("/compras/<int:id>/anular", methods=["POST"])
@rol_requerido("admin")
def compra_anular(id):
    c = db.session.get(CompraInsumo, id) or abort(404)
    try:
        anular_compra(c)
        db.session.commit()
        flash(f"Compra de {c.cantidad:g} {c.insumo.unidad} de «{c.insumo.nombre}» anulada. "
              "Se descontó del inventario.", "ok")
    except ErrorNegocio as e:
        db.session.rollback()
        flash(e.errores[0], "error")
    return redirect(request.referrer or url_for("compras"))


# -------------------------------------------------------------- referencias
@app.route("/productos")
@rol_requerido("admin", "produccion")
def productos():
    linea = request.args.get("linea", "")
    q = request.args.get("q", "").strip()
    query = Producto.query.filter_by(activo=True)
    if linea:
        query = query.filter(Producto.linea == linea)
    if q:
        query = query.filter((Producto.nombre.ilike(f"%{q}%")) | (Producto.codigo.ilike(f"%{q}%"))
                             | (Producto.color.ilike(f"%{q}%")))
    items = query.order_by(Producto.linea, Producto.nombre, Producto.color).all()
    return render_template("productos.html", items=items, linea=linea, q=q, lineas=LINEAS,
                           total_pares=sum(p.stock for p in items),
                           valor=sum(max(p.stock, 0) * p.costo_promedio for p in items))


@app.route("/productos/nuevo", methods=["GET", "POST"])
@app.route("/productos/<int:id>/editar", methods=["GET", "POST"])
@rol_requerido("admin")
def producto_form(id=None):
    prod = db.session.get(Producto, id) if id else None
    if id and not prod:
        abort(404)
    if request.method == "POST":
        f = request.form
        nombre = f.get("nombre", "").strip()
        color = f.get("color", "").strip()
        dup = Producto.query.filter(func.lower(Producto.nombre) == nombre.lower(),
                                    func.lower(func.coalesce(Producto.color, "")) == color.lower(),
                                    Producto.activo.is_(True), Producto.id != (id or 0)).first()
        rinde = n(f.get("tanda_rinde"), 1) or 1
        tmin, tmax = int(n(f.get("talla_min"))), int(n(f.get("talla_max")))
        receta = leer_lineas(f.getlist("receta_insumo[]"), f.getlist("receta_cantidad[]"),
                             f.getlist("receta_unidad[]"), divisor=rinde)
        procesos = [(p.strip()[:40], n(v)) for p, v in zip(f.getlist("proc_nombre[]"), f.getlist("proc_valor[]"))
                    if p.strip() and n(v) > 0]
        if not nombre:
            flash("Escribe el nombre de la referencia.", "error")
        elif rinde <= 0:
            flash("La ficha debe rendir al menos 1 par.", "error")
        elif tmin <= 0 or tmax <= 0 or tmax < tmin or tmax - tmin > 25:
            flash("Revisa el rango de tallas (ej. de 35 a 40).", "error")
        elif dup:
            flash("Ya existe una referencia activa con ese nombre y color.", "error")
        else:
            try:
                nuevo = prod is None
                if nuevo:
                    prod = Producto()
                    db.session.add(prod)
                prod.nombre, prod.color = nombre[:120], color[:60] or None
                prod.codigo = f.get("codigo", "").strip()[:30] or None
                prod.linea = f.get("linea") if f.get("linea") in LINEAS else LINEAS[0]
                prod.material = f.get("material", "").strip()[:80] or None
                prod.descripcion = f.get("descripcion", "").strip()[:255] or None
                prod.talla_min, prod.talla_max = tmin, tmax
                prod.precio_venta = n(f.get("precio_venta"))
                prod.precio_mayor = n(f.get("precio_mayor"))
                prod.stock_minimo = int(n(f.get("stock_minimo")))
                prod.tanda_rinde = rinde
                prod.receta.clear()
                prod.procesos.clear()
                db.session.flush()
                for ins, (c, u) in receta.items():
                    prod.receta.append(RecetaItem(insumo=ins, cantidad=c, unidad=u))
                for p, v in procesos:
                    prod.procesos.append(ProcesoTarifa(proceso=p, valor_par=v))
                if nuevo:
                    corrida = leer_corrida(f, "inicial")
                    if corrida:
                        costo_u = n(f.get("costo_inicial")) or prod.costo_estandar
                        registrar_produccion(prod, corrida, {}, [], otros=costo_u * sum(corrida.values()),
                                             otros_descripcion="Inventario inicial", nota="Inventario inicial",
                                             usuario_id=current_user.id, sin_materiales=True, es_inicial=True)
                db.session.commit()
                flash(f"Referencia «{prod.etiqueta}» guardada.", "ok")
                return redirect(url_for("producto_detalle", id=prod.id))
            except ErrorNegocio as e:
                db.session.rollback()
                for m in e.errores:
                    flash(m, "error")
    receta = receta_json(prod.receta, prod.tanda_rinde or 1) if prod else []
    procs = [dict(proceso=p.proceso, valor=p.valor_par) for p in prod.procesos] if prod else []
    return render_template("producto_form.html", prod=prod, insumos=insumos_json(), receta=receta,
                           procesos=procs, lista_procesos=PROCESOS, lineas=LINEAS)


# ---------------------------------------------------------------- importar
MAX_IMPORT = 5 * 1024 * 1024


def _lote(id):
    lote = db.session.get(ImportLote, id) or abort(404)
    return lote


@app.route("/importar", methods=["GET", "POST"])
@rol_requerido("admin")
def importar():
    if request.method == "POST":
        arch = request.files.get("archivo")
        if not arch or not arch.filename:
            flash("Elige el archivo de Excel que vas a subir.", "error")
            return redirect(url_for("importar"))
        if not arch.filename.lower().endswith((".xlsx", ".xlsm")):
            flash("El archivo debe ser de Excel (.xlsx). Si es .xls o .csv, ábrelo en Excel y guárdalo "
                  "como «Libro de Excel (.xlsx)».", "error")
            return redirect(url_for("importar"))
        contenido = arch.read(MAX_IMPORT + 1)
        if len(contenido) > MAX_IMPORT:
            flash("El archivo pesa más de 5 MB. Quita hojas o imágenes que no sean de la plantilla.", "error")
            return redirect(url_for("importar"))
        fecha_saldos = fecha_form(request.form.get("fecha")).date()
        try:
            datos, avisos = importador.leer(contenido)
        except ErrorNegocio as e:
            for m in e.errores:
                flash(m, "error")
            return redirect(url_for("importar"))
        if not datos:
            for m in avisos:
                flash(m, "error")
            flash("No se encontraron datos. Usa la plantilla y escribe desde la fila 3 de cada hoja.", "error")
            return redirect(url_for("importar"))
        # Ensayo completo: se aplica todo y se deshace, para mostrar la revisión real.
        imp = importador.Importacion(datos, current_user.id, fecha_saldos)
        try:
            imp.correr()
        except Exception as e:  # cualquier sorpresa se muestra como error, nunca se guarda a medias
            imp.errores.append(("—", "—", f"Error inesperado: {str(e)[:200]}"))
        db.session.rollback()
        filas = {k: len(v) for k, v in datos.items()}
        suma = [k for k in ("existencias", "materiales") if k in imp.res and
                (imp.res[k].get("pares") or imp.res[k].get("valor_bodega"))]
        if suma:
            previo = ImportLote.query.filter_by(estado="aplicado").order_by(ImportLote.id.desc()).first()
            if previo:
                avisos.append(f"Ya importaste un archivo el {previo.fecha:%d/%m/%Y} («{previo.archivo_nombre}»). "
                              "Las cantidades de bodega se SUMAN: si este archivo trae las mismas existencias, "
                              "quedarían dobles. Si solo querías corregir datos, borra las columnas de cantidades "
                              "o deshaz primero la importación anterior.")
        lote = ImportLote(usuario_id=current_user.id, archivo_nombre=arch.filename[:160], archivo=contenido,
                          fecha_saldos=fecha_saldos, estado="revision",
                          resumen=importador.a_json(dict(res=imp.res, errores=imp.errores[:500],
                                                         n_errores=len(imp.errores), avisos=avisos, filas=filas)))
        db.session.add(lote)
        db.session.commit()
        return redirect(url_for("importar_revision", id=lote.id))
    lotes = ImportLote.query.order_by(ImportLote.id.desc()).limit(20).all()
    return render_template("importar.html", lotes=lotes, hojas=importador.HOJAS, json=__import__("json"),
                           resumen_txt=importador.resumen_txt)


@app.route("/importar/plantilla.xlsx")
@rol_requerido("admin")
def importar_plantilla():
    ej = request.args.get("ejemplo") == "1"
    return Response(importador.plantilla(ejemplo=ej),
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename="
                             + ("Ejemplo_importacion_CalzadoControl.xlsx" if ej else "Plantilla_importacion_CalzadoControl.xlsx")})


@app.route("/importar/<int:id>")
@rol_requerido("admin")
def importar_revision(id):
    import json
    lote = _lote(id)
    r = json.loads(lote.resumen or "{}")
    return render_template("importar_revision.html", lote=lote, r=r,
                           lineas=importador.resumen_txt(r.get("res", {})),
                           titulos={k: t for k, t, *_ in importador.HOJAS})


@app.route("/importar/<int:id>/confirmar", methods=["POST"])
@rol_requerido("admin")
def importar_confirmar(id):
    lote = _lote(id)
    if lote.estado != "revision":
        flash("Esta importación ya se procesó.", "error")
        return redirect(url_for("importar_revision", id=id))
    datos, avisos = importador.leer(lote.archivo)
    imp = importador.Importacion(datos, current_user.id, lote.fecha_saldos)
    try:
        ok = imp.correr()
    except Exception as e:
        ok = False
        imp.errores.append(("—", "—", f"Error inesperado: {str(e)[:200]}"))
    if not ok:
        db.session.rollback()
        lote = _lote(id)
        lote.resumen = importador.a_json(dict(res=imp.res, errores=imp.errores[:500], n_errores=len(imp.errores),
                                              avisos=avisos, filas={k: len(v) for k, v in datos.items()}))
        db.session.commit()
        flash("Los datos cambiaron desde la revisión y ahora hay errores. Revísalos abajo.", "error")
        return redirect(url_for("importar_revision", id=id))
    lote.estado = "aplicado"
    lote.items = importador.a_json(imp.creado)
    lote.fecha = ahora()
    lote.resumen = importador.a_json(dict(res=imp.res, errores=[], n_errores=0, avisos=avisos,
                                          filas={k: len(v) for k, v in datos.items()}))
    db.session.commit()
    flash("Importación terminada. Revisa las secciones; si algo quedó mal, puedes deshacerla desde Importar datos.", "ok")
    return redirect(url_for("importar_revision", id=id))


@app.route("/importar/<int:id>/deshacer", methods=["POST"])
@rol_requerido("admin")
def importar_deshacer(id):
    import json
    lote = _lote(id)
    if lote.estado != "aplicado":
        flash("Solo se puede deshacer una importación aplicada.", "error")
        return redirect(url_for("importar_revision", id=id))
    try:
        desact = importador.deshacer(json.loads(lote.items or "[]"))
        lote.estado = "deshecho"
        lote.deshecho_en = ahora()
        db.session.commit()
        msg = "Importación deshecha."
        if desact:
            msg += (f" {desact} registro(s) que ya se usaron en otras operaciones quedaron inactivos en vez de borrarse.")
        flash(msg + " Los datos que ya existían y se actualizaron conservan los valores nuevos.", "ok")
    except ErrorNegocio as e:
        db.session.rollback()
        flash("No se pudo deshacer:", "error")
        for m in e.errores[:15]:
            flash(m, "error")
    return redirect(url_for("importar_revision", id=id))


@app.route("/importar/<int:id>/descartar", methods=["POST"])
@rol_requerido("admin")
def importar_descartar(id):
    lote = _lote(id)
    if lote.estado == "revision":
        lote.estado = "descartado"
        lote.archivo = None
        db.session.commit()
    return redirect(url_for("importar"))


@app.route("/productos/existencias", methods=["GET", "POST"])
@rol_requerido("admin")
def producto_existencias():
    """Carga de pares terminados que ya estaban en bodega al empezar a usar la app."""
    prods = Producto.query.filter_by(activo=True).order_by(Producto.linea, Producto.nombre, Producto.color).all()
    if request.method == "POST":
        f = request.form
        fecha = fecha_form(f.get("fecha"))
        cargas, errores = [], []
        for p in prods:
            corrida = {}
            for t in p.rango_tallas:
                c = int(n(f.get(f"c_{p.id}_{t}")))
                if c < 0:
                    errores.append(f"«{p.etiqueta}» talla {t}: la cantidad no puede ser negativa.")
                elif c:
                    corrida[t] = c
            if not corrida:
                continue
            costo = n(f.get(f"costo_{p.id}"))
            if costo <= 0:
                errores.append(f"Escribe el costo por par de «{p.etiqueta}».")
                continue
            cargas.append((p, corrida, costo))
        if not cargas and not errores:
            errores.append("Escribe al menos una cantidad en alguna talla.")
        if errores:
            for m in errores:
                flash(m, "error")
        else:
            try:
                pares = 0
                for p, corrida, costo in cargas:
                    q = sum(corrida.values())
                    registrar_produccion(p, corrida, {}, [], otros=costo * q, otros_descripcion="Inventario inicial",
                                         nota="Inventario inicial", fecha=fecha, usuario_id=current_user.id,
                                         sin_materiales=True, es_inicial=True)
                    pares += q
                db.session.commit()
                flash(f"Existencias cargadas: {pares} pares en {len(cargas)} referencia(s). "
                      "Ya se pueden vender; no cuentan como producción del periodo.", "ok")
                return redirect(url_for("productos"))
            except ErrorNegocio as e:
                db.session.rollback()
                for m in e.errores:
                    flash(m, "error")
    return render_template("existencias.html", prods=prods, f=request.form)


@app.route("/productos/<int:id>/duplicar", methods=["POST"])
@rol_requerido("admin")
def producto_duplicar(id):
    """Crea otro color de la misma referencia con la misma ficha y tarifas."""
    base = db.session.get(Producto, id) or abort(404)
    nuevo = Producto(codigo=base.codigo, nombre=base.nombre, linea=base.linea, color=None,
                     material=base.material, descripcion=base.descripcion, talla_min=base.talla_min,
                     talla_max=base.talla_max, precio_venta=base.precio_venta, precio_mayor=base.precio_mayor,
                     stock_minimo=base.stock_minimo, tanda_rinde=base.tanda_rinde, activo=False)
    db.session.add(nuevo)
    db.session.flush()
    for r in base.receta:
        nuevo.receta.append(RecetaItem(insumo=r.insumo, cantidad=r.cantidad, unidad=r.unidad))
    for p in base.procesos:
        nuevo.procesos.append(ProcesoTarifa(proceso=p.proceso, valor_par=p.valor_par))
    nuevo.activo = True
    nuevo.color = "Nuevo color"
    db.session.commit()
    flash("Copia creada con la misma ficha y tarifas. Cambia el color (y lo que necesites) y guarda.", "info")
    return redirect(url_for("producto_form", id=nuevo.id))


@app.route("/productos/<int:id>")
@rol_requerido("admin", "produccion")
def producto_detalle(id):
    prod = db.session.get(Producto, id) or abort(404)
    prods = (Produccion.query.filter_by(producto_id=id)
             .order_by(Produccion.fecha.desc()).limit(10).all())
    vendidos = (db.session.query(func.coalesce(func.sum(VentaItem.cantidad), 0),
                                 func.coalesce(func.sum(VentaItem.subtotal - VentaItem.costo), 0))
                .select_from(VentaItem).join(Venta, VentaItem.venta_id == Venta.id)
                .filter(VentaItem.producto_id == id, Venta.anulada.is_(False)).first())
    por_talla = dict(db.session.query(VentaItem.talla, func.sum(VentaItem.cantidad))
                     .join(Venta, VentaItem.venta_id == Venta.id)
                     .filter(VentaItem.producto_id == id, Venta.anulada.is_(False))
                     .group_by(VentaItem.talla).all())
    return render_template("producto_detalle.html", prod=prod, prods=prods,
                           vendidos=int(vendidos[0]), ganancia_hist=vendidos[1], vend_talla=por_talla)


@app.route("/productos/<int:id>/eliminar", methods=["POST"])
@rol_requerido("admin")
def producto_eliminar(id):
    prod = db.session.get(Producto, id) or abort(404)
    prod.activo = False
    db.session.commit()
    flash(f"Referencia «{prod.etiqueta}» eliminada (su historial se conserva).", "ok")
    return redirect(url_for("productos"))


# --------------------------------------------------------------- producción
@app.route("/produccion")
@rol_requerido("admin", "produccion")
def producciones():
    ini, fin, desde, hasta = periodo()
    items = (Produccion.query.filter(Produccion.fecha >= ini, Produccion.fecha < fin)
             .order_by(Produccion.fecha.desc()).all())
    validas = [p for p in items if not p.anulada and not p.es_inicial]
    unidades = sum(p.cantidad for p in validas)
    costo = sum(p.costo_total for p in validas)
    mo = sum(p.costo_mano_obra for p in validas)
    return render_template("producciones.html", items=items, desde=desde, hasta=hasta,
                           unidades=unidades, costo=costo, mo=mo)


def _productos_para_produccion():
    return [dict(id=p.id, nombre=p.etiqueta, precio=p.precio_venta, tallas=p.rango_tallas,
                 rinde=1, receta=receta_json(p.receta),
                 procesos=[dict(proceso=x.proceso, valor=x.valor_par) for x in p.procesos])
            for p in Producto.query.filter_by(activo=True).order_by(Producto.nombre, Producto.color)]


@app.route("/produccion/nueva", methods=["GET", "POST"])
@rol_requerido("admin", "produccion")
def produccion_nueva():
    prefill = None
    if request.method == "POST":
        f = request.form
        prod = db.session.get(Producto, int(n(f.get("producto_id"))))
        corrida = leer_corrida(f)
        lineas = leer_lineas(f.getlist("consumo_insumo[]"), f.getlist("consumo_cantidad[]"),
                             f.getlist("consumo_unidad[]"))
        consumos = {i: c for i, (c, _) in lineas.items()}
        mo = []
        for proc, emp, resp, modo, val, pares in zip(
                f.getlist("mo_proceso[]"), f.getlist("mo_empleado[]"), f.getlist("mo_responsable[]"),
                f.getlist("mo_modo[]"), f.getlist("mo_valor[]"), f.getlist("mo_pares[]")):
            e = db.session.get(Empleado, int(n(emp))) if n(emp) else None
            mo.append(dict(proceso=proc.strip(), empleado=e, responsable=resp, modo=modo, valor=n(val),
                           pares=int(n(pares))))
        try:
            p = registrar_produccion(
                prod, corrida, consumos, mo,
                energia=n(f.get("costo_energia")), gas_agua=n(f.get("costo_gas_agua")),
                transporte=n(f.get("costo_transporte")), otros=n(f.get("costo_otros")),
                otros_descripcion=f.get("otros_descripcion", ""), nota=f.get("nota", "").strip(),
                fecha=fecha_form(f.get("fecha")), usuario_id=current_user.id)
            db.session.commit()
            flash(f"Producción registrada: {p.cantidad} pares de «{p.producto.etiqueta}» "
                  f"a {filtro_cop(p.costo_unitario)} el par.", "ok")
            return redirect(url_for("produccion_detalle", id=p.id))
        except ErrorNegocio as e:
            db.session.rollback()
            for m in e.errores:
                flash(m, "error")
            prefill = {k: f.get(k, "") for k in (
                "producto_id", "fecha", "costo_energia", "costo_gas_agua", "costo_transporte",
                "costo_otros", "otros_descripcion", "nota")}
            prefill["corrida"] = {str(k): v for k, v in corrida.items()}
            prefill["consumos"] = [dict(insumo_id=i.id, unidad=u, cantidad=round(convertir(c, i.unidad, u), 6))
                                   for i, (c, u) in lineas.items()]
            prefill["mo"] = [dict(proceso=m["proceso"], empleado=m["empleado"].id if m["empleado"] else "",
                                  responsable=m["responsable"], modo=m["modo"], valor=m["valor"],
                                  pares=m["pares"]) for m in mo]
    return render_template("produccion_form.html", productos=_productos_para_produccion(),
                           insumos=insumos_json(), prefill=prefill, empleados=empleados_json(),
                           procesos=PROCESOS, modos=MODOS_MO,
                           preseleccion=request.args.get("producto", type=int))


@app.route("/produccion/<int:id>")
@rol_requerido("admin", "produccion")
def produccion_detalle(id):
    p = db.session.get(Produccion, id) or abort(404)
    usados = {c.insumo_id: c.cantidad for c in p.consumos}
    teorico, limitante = None, None
    for r in p.producto.receta:
        if r.cantidad > 0 and p.consumos:
            posible = usados.get(r.insumo_id, 0) / r.cantidad
            if teorico is None or posible < teorico:
                teorico, limitante = posible, r.insumo
    return render_template("produccion_detalle.html", p=p, teorico=teorico, limitante=limitante,
                           modos=MODOS_MO)


@app.route("/produccion/<int:id>/editar", methods=["GET", "POST"])
@rol_requerido("admin", "produccion")
def produccion_editar(id):
    p = db.session.get(Produccion, id) or abort(404)
    r = _editar_fecha_nota(p, url_for("produccion_detalle", id=id))
    if r:
        return r
    return render_template("editar_fecha.html", titulo=f"Editar producción #{p.id}",
                           detalle=f"{p.producto.etiqueta} · {p.cantidad} pares · {filtro_cop(p.costo_total)}",
                           obj=p, volver=url_for("produccion_detalle", id=id),
                           ayuda="Si la corrida, los materiales o los costos están mal, se anula la producción y se registra de nuevo.")


@app.route("/produccion/<int:id>/anular", methods=["POST"])
@rol_requerido("admin")
def produccion_anular(id):
    p = db.session.get(Produccion, id) or abort(404)
    try:
        anular_produccion(p)
        db.session.commit()
        flash(f"Producción #{p.id} anulada: se quitaron {p.cantidad} pares y los materiales "
              "volvieron a la bodega.", "ok")
    except ErrorNegocio as e:
        db.session.rollback()
        for m in e.errores:
            flash(m, "error")
    return redirect(url_for("produccion_detalle", id=id))


# ------------------------------------------------------------ semielaborados
@app.route("/preparaciones")
@rol_requerido("admin", "produccion")
def preparaciones():
    ini, fin, desde, hasta = periodo()
    items = (Preparacion.query.filter(Preparacion.fecha >= ini, Preparacion.fecha < fin)
             .order_by(Preparacion.fecha.desc()).all())
    preps = Insumo.query.filter_by(activo=True, es_preparacion=True).order_by(Insumo.nombre).all()
    return render_template("preparaciones.html", items=items, preps=preps, desde=desde, hasta=hasta)


@app.route("/preparaciones/nueva", methods=["GET", "POST"])
@rol_requerido("admin", "produccion")
def preparacion_nueva():
    prefill = None
    if request.method == "POST":
        f = request.form
        prep = db.session.get(Insumo, int(f.get("insumo_id") or 0))
        lineas = leer_lineas(f.getlist("consumo_insumo[]"), f.getlist("consumo_cantidad[]"),
                             f.getlist("consumo_unidad[]"))
        consumos = {i: c for i, (c, _) in lineas.items()}
        filas = [dict(insumo_id=i.id, unidad=u, cantidad=round(convertir(c, i.unidad, u), 6))
                 for i, (c, u) in lineas.items()]
        try:
            p = registrar_preparacion(
                prep, n(f.get("cantidad")), consumos, mano_obra=n(f.get("mano_obra")),
                otros=n(f.get("otros")), otros_descripcion=f.get("otros_descripcion", ""),
                nota=f.get("nota", "").strip(), fecha=fecha_form(f.get("fecha")),
                usuario_id=current_user.id)
            db.session.commit()
            flash(f"Elaboración registrada: {filtro_num(p.cantidad, 3)} {p.insumo.unidad} de "
                  f"«{p.insumo.nombre}» a {filtro_cop(p.costo_unitario)} por {p.insumo.unidad}.", "ok")
            return redirect(url_for("preparacion_detalle", id=p.id))
        except ErrorNegocio as e:
            db.session.rollback()
            for m in e.errores:
                flash(m, "error")
            prefill = {k: f.get(k, "") for k in ("insumo_id", "cantidad", "tandas", "fecha", "mano_obra",
                                                 "otros", "otros_descripcion", "nota")}
            prefill["consumos"] = filas
    preps = [dict(id=i.id, nombre=i.nombre, unidad=i.unidad, stock=i.stock or 0, rinde=i.tanda_rinde or 1,
                  receta=receta_json(i.receta_prep, attr="componente"))
             for i in Insumo.query.filter_by(activo=True, es_preparacion=True).order_by(Insumo.nombre)]
    return render_template("preparacion_form.html", preps=preps,
                           insumos=insumos_json(solo_comprables=True), prefill=prefill,
                           preseleccion=request.args.get("insumo", type=int))


@app.route("/preparaciones/<int:id>")
@rol_requerido("admin", "produccion")
def preparacion_detalle(id):
    p = db.session.get(Preparacion, id) or abort(404)
    return render_template("preparacion_detalle.html", p=p)


def _editar_fecha_nota(obj, volver):
    """Formulario común para corregir la fecha y la nota de un movimiento."""
    if obj.anulada:
        flash("Este movimiento está anulado; no se puede editar.", "error")
        return redirect(volver)
    if request.method == "POST":
        nueva = _fecha(request.form.get("fecha"))
        if not nueva:
            flash("Indica una fecha válida.", "error")
        elif nueva > ahora().date():
            flash("La fecha no puede ser futura.", "error")
        else:
            if nueva != obj.fecha.date():
                obj.fecha = fecha_form(nueva.isoformat())
            obj.nota = request.form.get("nota", "").strip()[:255] or None
            db.session.commit()
            flash("Cambios guardados.", "ok")
            return redirect(volver)
    return None


@app.route("/preparaciones/<int:id>/editar", methods=["GET", "POST"])
@rol_requerido("admin", "produccion")
def preparacion_editar(id):
    p = db.session.get(Preparacion, id) or abort(404)
    r = _editar_fecha_nota(p, url_for("preparacion_detalle", id=id))
    if r:
        return r
    return render_template("editar_fecha.html", titulo=f"Editar elaboración #{p.id}",
                           detalle=f"{p.insumo.nombre} · {filtro_num(p.cantidad, 3)} {p.insumo.unidad} · "
                                   f"{filtro_cop(p.costo_total)}",
                           obj=p, volver=url_for("preparacion_detalle", id=id),
                           ayuda="Si la cantidad o los materiales están mal, se anula y se registra de nuevo.")


@app.route("/preparaciones/<int:id>/anular", methods=["POST"])
@rol_requerido("admin")
def preparacion_anular(id):
    p = db.session.get(Preparacion, id) or abort(404)
    try:
        anular_preparacion(p)
        db.session.commit()
        flash("Elaboración anulada: el semielaborado se descontó y los materiales volvieron a la bodega.", "ok")
    except ErrorNegocio as e:
        db.session.rollback()
        flash(e.errores[0], "error")
    return redirect(url_for("preparacion_detalle", id=id))


# ------------------------------------------------------------------ ventas
@app.route("/ventas")
@rol_requerido("admin", "vendedor")
def ventas():
    ini, fin, desde, hasta = periodo()
    cond = request.args.get("cond", "")
    q = Venta.query.filter(Venta.fecha >= ini, Venta.fecha < fin)
    if current_user.rol_efectivo == "vendedor":
        q = q.filter(Venta.usuario_id == current_user.id)
    if cond in ("contado", "credito"):
        q = q.filter(Venta.condicion == cond)
    items = q.order_by(Venta.fecha.desc()).all()
    validas = [v for v in items if not v.anulada]
    reales = [v for v in validas if not v.es_saldo_inicial]
    return render_template("ventas.html", items=items, desde=desde, hasta=hasta, cond=cond,
                           total=sum(v.total for v in reales),
                           ganancia=sum(v.ganancia for v in reales),
                           saldo=sum(v.saldo for v in validas),
                           unidades=sum(v.unidades for v in reales))


@app.route("/ventas/nueva", methods=["GET", "POST"])
@rol_requerido("admin", "vendedor")
def venta_nueva():
    neg = DatosNegocio.actual()
    if request.method == "POST":
        f = request.form
        items = []
        for pid, t, c, pr in zip(f.getlist("producto_id[]"), f.getlist("talla[]"), f.getlist("cantidad[]"),
                                 f.getlist("precio[]")):
            if pid:
                prod = db.session.get(Producto, int(pid))
                if prod:
                    items.append((prod, int(n(t)) if str(t).strip() else None, int(n(c)), n(pr)))
        nombre = f.get("cliente", "").strip()
        cli = None
        if f.get("cliente_id"):
            cli = db.session.get(Cliente, int(n(f.get("cliente_id"))))
        if not cli and nombre:
            cli = Cliente.query.filter(func.lower(Cliente.nombre) == nombre.lower(),
                                       Cliente.activo.is_(True)).first()
        if not cli and nombre and f.get("guardar_cliente"):
            cli = Cliente(nombre=nombre[:120], telefono=f.get("cliente_tel", "").strip()[:40] or None,
                          tipo=f.get("cliente_tipo") if f.get("cliente_tipo") in TIPOS_CLIENTE else "Otro")
            db.session.add(cli)
            db.session.flush()
        doc = f.get("cliente_doc", "").strip()[:40] or None
        if cli and doc and not cli.identificacion:
            cli.identificacion = doc
        try:
            v = registrar_venta(items, cli.nombre if cli else nombre, f.get("metodo_pago", "Efectivo"),
                                f.get("nota", "").strip(), fecha_form(f.get("fecha")), current_user.id,
                                cliente_id=cli.id if cli else None, condicion=f.get("condicion", "contado"),
                                plazo_dias=int(n(f.get("plazo_dias"))), descuento=n(f.get("descuento")),
                                iva_pct=n(f.get("iva_pct")), abono_inicial=n(f.get("abono_inicial")))
            v.cliente_doc = doc
            db.session.commit()
            extra = f" · saldo {filtro_cop(v.saldo)} vence {v.vence.strftime('%d/%m/%Y')}" if v.condicion == "credito" else ""
            flash(f"Venta {num_doc(v)} registrada por {filtro_cop(v.total_factura)}{extra}.", "ok")
            return redirect(url_for("venta_detalle", id=v.id))
        except ErrorNegocio as e:
            db.session.rollback()
            for m in e.errores:
                flash(m, "error")
    productos_data = [dict(id=p.id, nombre=p.etiqueta, linea=p.linea, precio=p.precio_venta,
                           mayor=p.precio_mayor or p.precio_venta, stock=p.stock,
                           tallas={str(t): s for t, s in p.stock_por_talla.items()})
                      for p in Producto.query.filter_by(activo=True).order_by(Producto.linea, Producto.nombre,
                                                                              Producto.color)]
    clis = Cliente.query.filter_by(activo=True).order_by(Cliente.nombre).all()
    met = ccalc.metricas(clis, ahora().date())
    clientes_data = [dict(id=c.id, nombre=c.nombre, tel=c.telefono or "", tipo=c.tipo or "",
                          doc=c.identificacion or "", plazo=c.plazo_dias or 0, cupo=c.cupo or 0,
                          mayor=bool(c.usa_precio_mayor), saldo=met[c.id]["saldo"],
                          vencido=met[c.id]["vencido"], estado=met[c.id]["estado_txt"],
                          dias=met[c.id]["dias"])
                     for c in clis]
    return render_template("venta_form.html", productos=productos_data, metodos=METODOS_PAGO,
                           clientes=clientes_data, tipos_cliente=TIPOS_CLIENTE, lineas=LINEAS,
                           iva_def=neg.iva_pct or 0, pre_cliente=request.args.get("cliente", type=int))


@app.route("/ventas/<int:id>")
@rol_requerido("admin", "vendedor")
def venta_detalle(id):
    v = db.session.get(Venta, id) or abort(404)
    if current_user.rol_efectivo == "vendedor" and v.usuario_id != current_user.id:
        abort(404)
    devs = Devolucion.query.filter_by(venta_id=id).order_by(Devolucion.fecha).all()
    cli = db.session.get(Cliente, v.cliente_id) if v.cliente_id else None
    return render_template("venta_detalle.html", v=v, devs=devs, metodos=METODOS_PAGO, cli=cli,
                           recordatorio=_texto_recordatorio(cli, [v]) if cli and v.saldo > 0.5 else None)


@app.route("/ventas/<int:id>/comprobante.<fmt>")
@rol_requerido("admin", "vendedor")
def venta_comprobante(id, fmt):
    v = db.session.get(Venta, id) or abort(404)
    if current_user.rol_efectivo == "vendedor" and v.usuario_id != current_user.id:
        abort(404)
    negocio = DatosNegocio.actual()
    nombre = f"{comprobantes.numero(v, negocio.prefijo)}.{fmt}"
    if fmt == "pdf":
        data, mime = comprobantes.pdf(v, negocio), "application/pdf"
    elif fmt == "jpg":
        data, mime = comprobantes.jpg(v, negocio), "image/jpeg"
    else:
        abort(404)
    modo = "attachment" if request.args.get("descargar") else "inline"
    return Response(data, mimetype=mime,
                    headers={"Content-Disposition": f'{modo}; filename="{nombre}"'})


@app.route("/ventas/<int:id>/devolucion", methods=["POST"])
@rol_requerido("admin")
def venta_devolucion(id):
    v = db.session.get(Venta, id) or abort(404)
    cant = {}
    for it in v.items:
        c = int(n(request.form.get(f"dev_{it.id}")))
        if c:
            cant[it] = c
    try:
        dev = devolver_parcial(v, cant, request.form.get("motivo", ""), current_user.id)
        db.session.commit()
        flash(f"Devolución registrada: {dev.unidades} par{'es' if dev.unidades != 1 else ''} volvieron al inventario y la venta "
              f"quedó en {filtro_cop(v.total_factura)}.", "ok")
    except ErrorNegocio as e:
        db.session.rollback()
        for m in e.errores:
            flash(m, "error")
    return redirect(url_for("venta_detalle", id=id))


@app.route("/ventas/<int:id>/anular", methods=["POST"])
@rol_requerido("admin")
def venta_anular(id):
    v = db.session.get(Venta, id) or abort(404)
    try:
        anular_venta(v)
        db.session.commit()
        flash(f"Venta {num_doc(v)} anulada. Los pares volvieron al inventario.", "ok")
    except ErrorNegocio as e:
        flash(e.errores[0], "error")
    return redirect(url_for("venta_detalle", id=id))


@app.route("/negocio", methods=["GET", "POST"])
@rol_requerido("admin")
def negocio():
    neg = DatosNegocio.actual()
    if request.method == "POST":
        f = request.form
        nombre = f.get("nombre", "").strip()
        iva = n(f.get("iva_pct"))
        archivo = request.files.get("logo")
        if not nombre:
            flash("El nombre del negocio es obligatorio.", "error")
        elif not 0 <= iva <= 100:
            flash("El IVA debe estar entre 0 y 100.", "error")
        else:
            neg.nombre = nombre[:120]
            for campo, largo in (("nit", 40), ("direccion", 160), ("ciudad", 80), ("telefono", 40),
                                 ("email", 120), ("regimen", 120), ("titulo_doc", 60), ("mensaje", 200)):
                setattr(neg, campo, (f.get(campo, "").strip()[:largo]) or None)
            neg.prefijo = "".join(ch for ch in f.get("prefijo", "CZ").upper() if ch.isalnum())[:8] or "CZ"
            neg.iva_pct = iva
            if f.get("quitar_logo"):
                neg.logo, neg.logo_v = None, (neg.logo_v or 0) + 1
            elif archivo and archivo.filename:
                try:
                    neg.logo = _procesar_logo(archivo.read())
                    neg.logo_v = (neg.logo_v or 0) + 1
                except Exception:
                    flash("No se pudo leer el logo. Sube una imagen JPG o PNG.", "error")
                    db.session.rollback()
                    return redirect(url_for("negocio"))
            db.session.commit()
            negocio_cache(refrescar=True)
            flash("Datos del negocio guardados. Así saldrán en los recibos.", "ok")
            return redirect(url_for("negocio"))
    ultima = Venta.query.order_by(Venta.id.desc()).first()
    return render_template("negocio.html", neg=neg, ultima=ultima)


def _procesar_logo(data):
    """Convierte la imagen subida en un JPG cuadrado de hasta 600 px con fondo blanco."""
    from PIL import Image
    import io
    Image.MAX_IMAGE_PIXELS = 40_000_000  # rechaza imágenes gigantes hechas para tumbar el servidor
    im = Image.open(io.BytesIO(data))
    if im.format not in ("JPEG", "PNG", "WEBP"):
        raise ValueError("formato no permitido")
    im.load()
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        fondo = Image.new("RGB", im.size, "white")
        fondo.paste(im, mask=im.split()[-1])
        im = fondo
    else:
        im = im.convert("RGB")
    im.thumbnail((600, 600))
    lado = max(im.size)
    lienzo = Image.new("RGB", (lado, lado), "white")
    lienzo.paste(im, ((lado - im.width) // 2, (lado - im.height) // 2))
    buf = io.BytesIO()
    lienzo.save(buf, "JPEG", quality=90)
    return buf.getvalue()


@app.route("/logo.jpg")
def logo():
    """Logo de la empresa activa (o el genérico de la app)."""
    if empresa_id_actual():
        neg = DatosNegocio.query.first()
        if neg and neg.logo:
            r = Response(neg.logo, mimetype="image/jpeg")
            r.headers["Cache-Control"] = "private, max-age=2592000"
            return r
    return send_file(os.path.join(app.root_path, "static", "logo_sm.jpg"), max_age=2592000)


@app.route("/bloqueado")
@login_required
def bloqueado():
    emp = getattr(g, "empresa", None)
    if not emp or not emp.bloqueada:
        return redirect(url_for("inicio"))
    return render_template("bloqueado.html", emp=emp, wa=wa_activar(emp))


# ---------------------------------------------------------------- empleados
@app.route("/empleados")
@rol_requerido("admin")
def empleados():
    ver = request.args.get("ver", "")
    q = Empleado.query
    if ver != "todos":
        q = q.filter_by(activo=True)
    items = q.order_by(Empleado.activo.desc(), Empleado.nombre).all()
    pend = dict(db.session.query(ProduccionMO.empleado_id, func.sum(ProduccionMO.total))
                .join(Produccion, ProduccionMO.produccion_id == Produccion.id)
                .filter(ProduccionMO.nomina_id.is_(None), Produccion.anulada.is_(False),
                        ProduccionMO.empleado_id.isnot(None))
                .group_by(ProduccionMO.empleado_id).all())
    ini_mes = ahora().date().replace(day=1)
    pares_mes = dict(db.session.query(ProduccionMO.empleado_id, func.sum(ProduccionMO.pares))
                     .join(Produccion, ProduccionMO.produccion_id == Produccion.id)
                     .filter(Produccion.anulada.is_(False), ProduccionMO.empleado_id.isnot(None),
                             Produccion.fecha >= datetime.combine(ini_mes, datetime.min.time()))
                     .group_by(ProduccionMO.empleado_id).all())
    return render_template("empleados.html", items=items, pend=pend, pares_mes=pares_mes, ver=ver,
                           total_pend=sum(v or 0 for v in pend.values()))


@app.route("/empleados/nuevo", methods=["GET", "POST"])
@app.route("/empleados/<int:id>/editar", methods=["GET", "POST"])
@rol_requerido("admin")
def empleado_form(id=None):
    e = db.session.get(Empleado, id) if id else None
    if id and not e:
        abort(404)
    if request.method == "POST":
        f = request.form
        nombre = f.get("nombre", "").strip()
        tipo = f.get("tipo_pago") if f.get("tipo_pago") in TIPOS_PAGO else "destajo"
        salario = n(f.get("salario"))
        if not nombre:
            flash("Escribe el nombre del empleado.", "error")
        elif tipo in ("fijo", "mixto") and salario <= 0:
            flash("Indica el salario mensual.", "error")
        else:
            if not e:
                e = Empleado()
                db.session.add(e)
            e.nombre = nombre[:120]
            for campo, largo in (("documento", 40), ("telefono", 40), ("cargo", 80), ("nota", 255)):
                setattr(e, campo, f.get(campo, "").strip()[:largo] or None)
            e.proceso = f.get("proceso") if f.get("proceso") in PROCESOS else None
            e.tipo_pago, e.salario = tipo, salario if tipo != "destajo" else 0
            e.aux_transporte = bool(f.get("aux_transporte"))
            e.fecha_ingreso = _fecha(f.get("fecha_ingreso"))
            e.activo = bool(f.get("activo")) if id else True
            db.session.commit()
            flash(f"Empleado «{e.nombre}» guardado.", "ok")
            return redirect(url_for("empleado_detalle", id=e.id))
    return render_template("empleado_form.html", e=e, procesos=PROCESOS, tipos=TIPOS_PAGO)


@app.route("/empleados/<int:id>")
@rol_requerido("admin")
def empleado_detalle(id):
    e = db.session.get(Empleado, id) or abort(404)
    lineas = (ProduccionMO.query.join(Produccion, ProduccionMO.produccion_id == Produccion.id)
              .filter(ProduccionMO.empleado_id == id, Produccion.anulada.is_(False))
              .order_by(Produccion.fecha.desc()).limit(40).all())
    pendiente = [l for l in lineas if not l.nomina_id]
    pagos = PagoNomina.query.filter_by(empleado_id=id).order_by(PagoNomina.fecha_pago.desc()).limit(24).all()
    return render_template("empleado_detalle.html", e=e, lineas=lineas, pagos=pagos,
                           total_pend=sum(l.total for l in pendiente), pares_pend=sum(l.pares for l in pendiente))


# ------------------------------------------------------------------- nómina
@app.route("/nomina")
@rol_requerido("admin")
def nominas():
    ini, fin, desde, hasta = periodo()
    items = (PagoNomina.query.filter(PagoNomina.fecha_pago >= ini.date(), PagoNomina.fecha_pago < fin.date())
             .order_by(PagoNomina.fecha_pago.desc(), PagoNomina.id.desc()).all())
    validas = [x for x in items if not x.anulada]
    return render_template("nominas.html", items=items, desde=desde, hasta=hasta,
                           neto=sum(x.neto for x in validas), costo=sum(x.costo_empresa for x in validas),
                           destajo=sum(x.destajo for x in validas), gasto=sum(x.gasto for x in validas))


@app.route("/nomina/nueva", methods=["GET", "POST"])
@rol_requerido("admin")
def nomina_nueva():
    cfg = Configuracion.actual()
    src = request.form if request.method == "POST" else request.args
    hoy_d = ahora().date()
    emp = db.session.get(Empleado, int(n(src.get("empleado_id")))) if n(src.get("empleado_id")) else None
    # Periodo por defecto: quincena actual
    d0 = hoy_d.replace(day=1) if hoy_d.day <= 15 else hoy_d.replace(day=16)
    h0 = hoy_d.replace(day=15) if hoy_d.day <= 15 else (hoy_d.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    desde = _fecha(src.get("desde"), d0)
    hasta = _fecha(src.get("hasta"), h0)
    dias_def = min((hasta - desde).days + 1, 30) if hasta >= desde else 0
    if desde.day == 16 and hasta.day >= 28 and hasta.month == desde.month:
        dias_def = 15  # segunda quincena: siempre 15 días (mes comercial de 30)
    dias = int(n(src.get("dias"), dias_def)) if src.get("dias") not in (None, "") else dias_def
    primera = not src.get("calc")
    ss = cfg.nom_aportes if primera else bool(src.get("seguridad_social"))
    pr = cfg.nom_prestaciones if primera else bool(src.get("prestaciones"))
    extras = dict(horas_extra=n(src.get("horas_extra")), bonificaciones=n(src.get("bonificaciones")),
                  otras_deducciones=n(src.get("otras_deducciones")))
    if request.method == "POST" and src.get("accion") == "registrar":
        try:
            pago = nom.registrar(emp, cfg, desde, hasta, _fecha(src.get("fecha_pago"), hoy_d), dias,
                                 deducciones_detalle=src.get("deducciones_detalle", ""),
                                 seguridad_social=ss, prestaciones=pr, metodo=src.get("metodo", "Efectivo"),
                                 nota=src.get("nota", ""), usuario_id=current_user.id, **extras)
            db.session.commit()
            flash(f"Nómina {pago.numero} de {emp.nombre} registrada: neto {filtro_cop(pago.neto)}.", "ok")
            return redirect(url_for("nomina_detalle", id=pago.id))
        except ErrorNegocio as e:
            db.session.rollback()
            for m in e.errores:
                flash(m, "error")
    lineas = nom.destajo_pendiente(emp, desde, hasta) if emp and emp.tipo_pago != "fijo" else []
    calc = nom.calcular(emp, cfg, dias, sum(l.total for l in lineas), seguridad_social=ss, prestaciones=pr,
                        **extras) if emp else None
    return render_template("nomina_form.html", emp=emp, empleados=Empleado.query.filter_by(activo=True)
                           .order_by(Empleado.nombre).all(), desde=desde.isoformat(), hasta=hasta.isoformat(),
                           dias=dias, ss=ss, pr=pr, extras=extras, lineas=lineas, calc=calc, cfg=cfg,
                           metodos=METODOS_PAGO + ["Consignación"], src=src)


@app.route("/nomina/<int:id>")
@rol_requerido("admin")
def nomina_detalle(id):
    p = db.session.get(PagoNomina, id) or abort(404)
    return render_template("nomina_detalle.html", p=p)


@app.route("/nomina/<int:id>/desprendible.pdf")
@rol_requerido("admin")
def nomina_pdf(id):
    p = db.session.get(PagoNomina, id) or abort(404)
    modo = "attachment" if request.args.get("descargar") else "inline"
    return Response(comprobantes.nomina_pdf(p, DatosNegocio.actual()), mimetype="application/pdf",
                    headers={"Content-Disposition": f'{modo}; filename="{p.numero}.pdf"'})


@app.route("/nomina/<int:id>/anular", methods=["POST"])
@rol_requerido("admin")
def nomina_anular(id):
    p = db.session.get(PagoNomina, id) or abort(404)
    try:
        nom.anular(p)
        db.session.commit()
        flash(f"Nómina {p.numero} anulada. El destajo de ese periodo vuelve a quedar pendiente de pago.", "ok")
    except ErrorNegocio as e:
        db.session.rollback()
        flash(e.errores[0], "error")
    return redirect(url_for("nomina_detalle", id=id))


# ------------------------------------------------------- egresos / ajustes
@app.route("/ajustes")
@rol_requerido("admin", "produccion")
def ajustes():
    ini, fin, desde, hasta = periodo()
    filas = (Ajuste.query.filter(Ajuste.fecha >= ini, Ajuste.fecha < fin)
             .order_by(Ajuste.fecha.desc(), Ajuste.id.desc()).all())
    por_motivo = defaultdict(float)
    for a in filas:
        if not a.anulada:
            por_motivo[a.motivo] += a.total
    return render_template("ajustes.html", filas=filas, desde=desde, hasta=hasta,
                           total=sum(por_motivo.values()),
                           por_motivo=sorted(por_motivo.items(), key=lambda x: -x[1]))


@app.route("/ajustes/nuevo", methods=["GET", "POST"])
@rol_requerido("admin", "produccion")
def ajuste_nuevo():
    if request.method == "POST":
        f = request.form
        motivo = f.get("motivo", "")
        conteo = motivo == MOTIVOS_AJUSTE[0]
        lineas, errores = [], []
        for ref, c, u in zip(f.getlist("item[]"), f.getlist("cantidad[]"), f.getlist("unidad[]")):
            if not ref or not str(c).strip():
                continue
            partes = ref.split(":")
            tipo, iid = partes[0], int(n(partes[1] if len(partes) > 1 else 0))
            talla = int(n(partes[2])) if tipo == "p" and len(partes) > 2 else None
            obj = db.session.get(Insumo if tipo == "i" else Producto, iid)
            if not obj:
                continue
            q = n(c)
            if isinstance(obj, Insumo):
                q = convertir(q, unidad_valida(u, obj.unidad), obj.unidad)
                hay = obj.stock or 0
            else:
                hay = obj.stock_talla(talla)
            if conteo:
                dif = hay - q
                if dif < -1e-9:
                    nom_txt = obj.nombre if isinstance(obj, Insumo) else f"{obj.etiqueta} T{talla}"
                    errores.append(f"De «{nom_txt}» se contaron {q:g} y el sistema tiene {hay:g}. "
                                   "Si sobra, se registra como compra o producción.")
                    continue
                if dif <= 1e-9:
                    continue
                q = round(dif) if isinstance(obj, Producto) else dif
            lineas.append((obj, talla, q))
        try:
            if errores:
                raise ErrorNegocio(errores)
            if conteo and not lineas:
                raise ErrorNegocio("El conteo coincide con el sistema: no hay nada que ajustar.")
            a = registrar_ajuste(lineas, motivo, f.get("descripcion", ""), fecha_form(f.get("fecha")),
                                 current_user.id)
            db.session.commit()
            flash(f"Egreso {a.numero} registrado por {filtro_cop(a.total)}. El inventario se actualizó.", "ok")
            return redirect(url_for("ajuste_detalle", id=a.id))
        except ErrorNegocio as e:
            db.session.rollback()
            for m in e.errores:
                flash(m, "error")
    items = [dict(ref=f"i:{i['id']}", nombre=i["nombre"], unidad=i["unidad"], stock=i["stock"],
                  costo=i["costo"], grupo="Semielaborados" if i["prep"] else f"Materiales · {i['cat']}")
             for i in insumos_json()]
    for p in Producto.query.filter_by(activo=True).order_by(Producto.nombre, Producto.color):
        for t, s in p.stock_por_talla.items():
            items.append(dict(ref=f"p:{p.id}:{t}", nombre=f"{p.etiqueta} · talla {t}", unidad="pares",
                              stock=s, costo=p.costo_promedio or 0, grupo="Calzado terminado"))
    pre = ("i:" + request.args["insumo"]) if request.args.get("insumo") else ""
    if request.args.get("producto"):
        p = db.session.get(Producto, request.args.get("producto", type=int))
        if p:
            con = [t for t, s in p.stock_por_talla.items() if s > 0] or p.rango_tallas
            pre = f"p:{p.id}:{con[0]}" if con else ""
    return render_template("ajuste_form.html", items=items, motivos=MOTIVOS_AJUSTE, pre=pre)


@app.route("/ajustes/<int:id>")
@rol_requerido("admin", "produccion")
def ajuste_detalle(id):
    a = db.session.get(Ajuste, id) or abort(404)
    return render_template("ajuste_detalle.html", a=a)


@app.route("/ajustes/<int:id>/documento.pdf")
@rol_requerido("admin", "produccion")
def ajuste_pdf(id):
    a = db.session.get(Ajuste, id) or abort(404)
    modo = "attachment" if request.args.get("descargar") else "inline"
    return Response(comprobantes.egreso_pdf(a, DatosNegocio.actual()), mimetype="application/pdf",
                    headers={"Content-Disposition": f'{modo}; filename="{a.numero}.pdf"'})


@app.route("/ajustes/<int:id>/anular", methods=["POST"])
@rol_requerido("admin")
def ajuste_anular(id):
    a = db.session.get(Ajuste, id) or abort(404)
    try:
        anular_ajuste(a)
        db.session.commit()
        flash(f"Egreso {a.numero} anulado. Las cantidades regresaron a la bodega.", "ok")
    except ErrorNegocio as e:
        db.session.rollback()
        for m in e.errores:
            flash(m, "error")
    return redirect(url_for("ajuste_detalle", id=id))


# ---------------------------------------------------------------- usuarios
@app.route("/usuarios")
@rol_requerido("admin")
def usuarios():
    return render_template("usuarios.html",
                           items=Usuario.query.filter_by(empresa_id=empresa_id_actual())
                           .order_by(Usuario.activo.desc(), Usuario.nombre).all())


@app.route("/usuarios/nuevo", methods=["GET", "POST"])
@app.route("/usuarios/<int:id>/editar", methods=["GET", "POST"])
@rol_requerido("admin")
def usuario_form(id=None):
    u = db.session.get(Usuario, id) if id else None
    if id and (not u or u.empresa_id != empresa_id_actual()):
        abort(404)
    if request.method == "POST":
        f = request.form
        nombre, username = f.get("nombre", "").strip(), f.get("username", "").strip().lower()
        rol, clave = f.get("rol", "vendedor"), f.get("password", "")
        activo = bool(f.get("activo")) if u else True
        dup = Usuario.query.filter(func.lower(Usuario.username) == username,
                                   Usuario.id != (id or 0)).first()
        if not nombre or not username:
            flash("Nombre y usuario son obligatorios.", "error")
        elif dup:
            flash("Ese nombre de usuario ya está en uso (puede ser de otra empresa). Prueba con otro, "
                  "por ejemplo agregando el nombre de tu negocio.", "error")
        elif rol not in ROLES:
            flash("Rol no válido.", "error")
        elif not u and len(clave) < 6:
            flash("La contraseña debe tener al menos 6 caracteres.", "error")
        elif u and u.id == current_user.id and (rol != "admin" or not activo):
            flash("No puedes quitarte el rol de administrador ni desactivarte a ti mismo.", "error")
        else:
            if not u:
                u = Usuario(empresa_id=empresa_id_actual())
                db.session.add(u)
            u.nombre, u.username, u.rol, u.activo = nombre, username, rol, activo
            if clave:
                if len(clave) < 6:
                    flash("La contraseña debe tener al menos 6 caracteres.", "error")
                    return render_template("usuario_form.html", u=u)
                u.set_password(clave)
            db.session.commit()
            flash(f"Usuario «{username}» guardado.", "ok")
            return redirect(url_for("usuarios"))
    return render_template("usuario_form.html", u=u)


# ------------------------------------------------------------ panel SoftGest
def sg_requerido(f):
    @wraps(f)
    @login_required
    def envoltura(*a, **kw):
        if current_user.rol != "superadmin":
            abort(404)
        return f(*a, **kw)
    return envoltura


def _slug(texto):
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    base = "".join(ch for ch in t if ch.isalnum())[:20] or "empresa"
    slug, k = base, 2
    while Empresa.query.filter_by(slug=slug).first():
        slug, k = f"{base}{k}", k + 1
    return slug


def crear_empresa(nombre, estado, dias, admin_user, admin_password, contacto=None, telefono=None, nota=None):
    """Crea la empresa con sus datos de negocio, configuración y su usuario administrador."""
    emp = Empresa(nombre=nombre[:120], slug=_slug(nombre), estado=estado, contacto=contacto, telefono=telefono,
                  nota=nota, vence=(ahora().date() + timedelta(days=dias)) if dias else None)
    db.session.add(emp)
    db.session.flush()
    db.session.add(DatosNegocio(empresa_id=emp.id, nombre=emp.nombre, telefono=telefono))
    db.session.add(Configuracion(empresa_id=emp.id))
    adm = Usuario(nombre=contacto or "Administrador", username=(admin_user or f"{emp.slug}.admin").lower(),
                  rol="admin", empresa_id=emp.id)
    adm.set_password(admin_password)
    db.session.add(adm)
    db.session.commit()
    return emp, adm


def _cargar_demo_en_segundo_plano(eid, admin_id):
    """La demo hace miles de registros: se carga en un hilo para no bloquear la petición."""
    def correr():
        with app.app_context():
            set_empresa(eid)
            try:
                demo.cargar(db.session.get(Empresa, eid), db.session.get(Usuario, admin_id))
            except Exception as e:
                db.session.rollback()
                print("Error cargando la demo:", e, flush=True)
            finally:
                emp = db.session.get(Empresa, eid)
                emp.demo_cargando = False
                db.session.commit()
                db.session.remove()
                set_empresa(None)
    threading.Thread(target=correr, daemon=True).start()


# Orden de borrado (primero los hijos) para vaciar los datos de una empresa
ORDEN_BORRADO = ["ImportLote", "AjusteItem", "Ajuste", "DevolucionItem", "Devolucion", "Abono", "VentaItem", "Venta",
                 "ProduccionMO", "PagoNomina", "ProduccionConsumo", "ProduccionTalla", "Produccion",
                 "PreparacionConsumo", "Preparacion", "CompraInsumo", "RecetaItem", "ProcesoTarifa", "ProductoTalla",
                 "Producto", "PrepRecetaItem", "Insumo", "Empleado", "Cliente", "Gasto", "Activo"]


def vaciar_empresa(eid):
    import models
    for nombre in ORDEN_BORRADO:
        m = getattr(models, nombre)
        db.session.query(m).filter(m.empresa_id == eid).delete(synchronize_session=False)
    demo_users = Usuario.query.filter(Usuario.empresa_id == eid,
                                      (Usuario.username.like("%.produccion")) | (Usuario.username.like("%.ventas"))).all()
    for u in demo_users:
        db.session.delete(u)
    db.session.commit()


def _resumen_empresas():
    """Datos de uso de cada empresa (sin filtro de empresa)."""
    def por_empresa(q):
        return dict(q.execution_options(todas_empresas=True).all())
    ultima = por_empresa(db.session.query(Venta.empresa_id, func.max(Venta.fecha)).group_by(Venta.empresa_id))
    nventas = por_empresa(db.session.query(Venta.empresa_id, func.count(Venta.id)).group_by(Venta.empresa_id))
    nprod = por_empresa(db.session.query(Producto.empresa_id, func.count(Producto.id)).group_by(Producto.empresa_id))
    nusers = dict(db.session.query(Usuario.empresa_id, func.count(Usuario.id)).group_by(Usuario.empresa_id).all())
    return ultima, nventas, nprod, nusers


@app.route("/softgest")
@sg_requerido
def sg_panel():
    session.pop("soporte_empresa", None)
    set_empresa(None)
    empresas = Empresa.query.order_by(Empresa.creado.desc()).all()
    ultima, nventas, nprod, nusers = _resumen_empresas()
    admins = {}
    for u in Usuario.query.filter(Usuario.rol == "admin").order_by(Usuario.id).all():
        admins.setdefault(u.empresa_id, u)
    return render_template("sg_panel.html", empresas=empresas, ultima=ultima, nventas=nventas, nprod=nprod,
                           nusers=nusers, admins=admins,
                           cuenta={k: sum(1 for e in empresas if e.estado == k) for k in ESTADOS_EMPRESA},
                           vencidas=sum(1 for e in empresas if e.bloqueada and e.estado != "suspendida"))


@app.route("/softgest/nueva", methods=["GET", "POST"])
@sg_requerido
def sg_nueva():
    f = request.form
    if request.method == "POST":
        nombre = f.get("nombre", "").strip()
        user = f.get("admin_user", "").strip().lower()
        clave = f.get("admin_password", "")
        estado = f.get("estado") if f.get("estado") in ("demo", "activa") else "demo"
        dias = int(n(f.get("dias"))) if estado == "demo" or f.get("con_vencimiento") else 0
        if not nombre:
            flash("Escribe el nombre de la empresa.", "error")
        elif not user or " " in user:
            flash("Escribe el usuario del administrador (sin espacios).", "error")
        elif Usuario.query.filter(func.lower(Usuario.username) == user).first():
            flash("Ese usuario ya existe. Usa otro (ej: nombre del negocio + .admin).", "error")
        elif len(clave) < 6:
            flash("La contraseña debe tener al menos 6 caracteres.", "error")
        elif estado == "demo" and dias < 1:
            flash("Indica cuántos días dura la demo.", "error")
        else:
            emp, adm = crear_empresa(nombre, estado, dias, user, clave, f.get("contacto", "").strip() or None,
                                     f.get("telefono", "").strip() or None, f.get("nota", "").strip() or None)
            msg = f"Empresa «{emp.nombre}» creada. Usuario: {adm.username} · clave: la que escribiste."
            if f.get("cargar_demo"):
                emp.demo_cargando = True
                db.session.commit()
                _cargar_demo_en_segundo_plano(emp.id, adm.id)
                msg += " Los datos de ejemplo se están cargando (1 a 3 minutos)."
            flash(msg, "ok")
            return redirect(url_for("sg_panel"))
    return render_template("sg_empresa.html", e=None, estados=ESTADOS_EMPRESA, f=f)


@app.route("/softgest/<int:id>", methods=["GET", "POST"])
@sg_requerido
def sg_empresa(id):
    e = db.session.get(Empresa, id) or abort(404)
    f = request.form
    if request.method == "POST":
        accion = f.get("accion", "guardar")
        hoy_d = ahora().date()
        if accion == "guardar":
            e.nombre = f.get("nombre", e.nombre).strip()[:120] or e.nombre
            e.contacto = f.get("contacto", "").strip()[:120] or None
            e.telefono = f.get("telefono", "").strip()[:40] or None
            e.nota = f.get("nota", "").strip()[:255] or None
            if f.get("estado") in ESTADOS_EMPRESA:
                e.estado = f.get("estado")
            e.vence = _fecha(f.get("vence"))
            flash("Cambios guardados.", "ok")
        elif accion.startswith("extender"):
            dias = int(accion.split(":")[1])
            base = max(e.vence or hoy_d, hoy_d)
            e.vence = base + timedelta(days=dias)
            if e.estado == "suspendida":
                e.estado = "demo"
            flash(f"Acceso extendido hasta el {e.vence.strftime('%d/%m/%Y')}.", "ok")
        elif accion == "activar":
            e.estado, e.vence = "activa", None
            flash(f"«{e.nombre}» activada sin vencimiento (pago único). Ya no ve el contador de demo.", "ok")
        elif accion == "suspender":
            e.estado = "suspendida"
            flash(f"«{e.nombre}» suspendida: sus usuarios ya no pueden entrar. Los datos se conservan.", "ok")
        elif accion == "clave":
            u = db.session.get(Usuario, int(n(f.get("usuario_id"))))
            clave = f.get("nueva_clave", "")
            if not u or u.empresa_id != e.id or len(clave) < 6:
                flash("Elige el usuario y escribe una clave de al menos 6 caracteres.", "error")
            else:
                u.set_password(clave)
                u.activo = True
                flash(f"Clave de {u.username} cambiada.", "ok")
        elif accion == "vaciar":
            if f.get("confirmar", "").strip().lower() != e.nombre.strip().lower():
                flash("Para vaciar escribe exactamente el nombre de la empresa.", "error")
            else:
                set_empresa(e.id)
                vaciar_empresa(e.id)
                set_empresa(None)
                flash(f"Datos de «{e.nombre}» vaciados. Quedan sus usuarios, datos del negocio y configuración.", "ok")
        elif accion == "demo":
            if e.demo_cargando:
                flash("La demo ya se está cargando.", "error")
            elif Producto.query.filter(Producto.empresa_id == e.id).execution_options(todas_empresas=True).first():
                flash("Esta empresa ya tiene referencias. Vacíala antes de cargar los datos de ejemplo.", "error")
            else:
                adm = Usuario.query.filter_by(empresa_id=e.id, rol="admin").first()
                e.demo_cargando = True
                db.session.commit()
                _cargar_demo_en_segundo_plano(e.id, adm.id)
                flash("Cargando datos de ejemplo (1 a 3 minutos). Recarga esta página para ver el avance.", "ok")
        db.session.commit()
        return redirect(url_for("sg_empresa", id=id))
    usuarios = Usuario.query.filter_by(empresa_id=id).order_by(Usuario.rol, Usuario.nombre).all()
    ultima, nventas, nprod, nusers = _resumen_empresas()
    return render_template("sg_empresa.html", e=e, estados=ESTADOS_EMPRESA, usuarios=usuarios, f=f,
                           uso=dict(ultima=ultima.get(id), ventas=nventas.get(id, 0), refs=nprod.get(id, 0)))


@app.route("/softgest/<int:id>/entrar", methods=["POST"])
@sg_requerido
def sg_entrar(id):
    e = db.session.get(Empresa, id) or abort(404)
    session["soporte_empresa"] = e.id
    flash(f"Estás dentro de «{e.nombre}» como soporte (con permisos de administrador).", "info")
    return redirect(url_for("inicio"))


@app.route("/softgest/salir")
@sg_requerido
def sg_salir():
    session.pop("soporte_empresa", None)
    return redirect(url_for("sg_panel"))


@app.errorhandler(404)
def no_encontrado(e):
    return render_template("error.html", titulo="No encontrado",
                           mensaje="Lo que buscas no existe o fue eliminado."), 404


# ------------------------------------------------------------- arranque BD
def migrar_columnas():
    """Agrega columnas nuevas a tablas que ya existían y pasa una instalación antigua
    (de una sola empresa) a multiempresa sin perder datos."""
    from sqlalchemy import inspect, text
    insp = inspect(db.engine)
    tablas = set(insp.get_table_names())
    nuevas = [("cz_usuarios", "empresa_id", "INTEGER"),
              ("cz_usuarios", "intentos_fallidos", "INTEGER NOT NULL DEFAULT 0"),
              ("cz_producciones", "es_inicial", "BOOLEAN NOT NULL DEFAULT FALSE"),
              ("cz_ventas", "origen", "VARCHAR(20)"),
              ("cz_usuarios", "bloqueado_hasta", "TIMESTAMP"), ("cz_ventas", "consecutivo", "INTEGER"),
              ("cz_ajustes", "consecutivo", "INTEGER"), ("cz_nomina", "consecutivo", "INTEGER"),
              ("cz_negocio", "logo", "BYTEA" if db.engine.dialect.name == "postgresql" else "BLOB"),
              ("cz_negocio", "logo_v", "INTEGER NOT NULL DEFAULT 0")]
    mapeadas = [m.class_ for m in db.Model.registry.mappers if issubclass(m.class_, EmpresaMixin)]
    nuevas += [(m.__tablename__, "empresa_id", "INTEGER") for m in mapeadas]
    for tabla, col, tipo in nuevas:
        if tabla in tablas and col not in {c["name"] for c in insp.get_columns(tabla)}:
            with db.engine.begin() as cn:
                cn.execute(text(f"ALTER TABLE {tabla} ADD COLUMN {col} {tipo}"))
    # Datos viejos sin empresa → empresa «Mi empresa»
    with db.engine.begin() as cn:
        huerfanos = cn.execute(text("SELECT COUNT(*) FROM cz_usuarios WHERE empresa_id IS NULL "
                                    "AND rol <> 'superadmin'")).scalar()
        if huerfanos:
            nombre = cn.execute(text("SELECT nombre FROM cz_negocio ORDER BY id LIMIT 1")).scalar() or "Mi empresa"
            cn.execute(text("INSERT INTO cz_empresas (nombre, slug, estado, demo_cargando, creado) "
                            "VALUES (:n, 'empresa1', 'activa', FALSE, :c)"), dict(n=nombre, c=ahora()))
            eid = cn.execute(text("SELECT MAX(id) FROM cz_empresas")).scalar()
            cn.execute(text("UPDATE cz_usuarios SET empresa_id = :e WHERE empresa_id IS NULL AND rol <> 'superadmin'"),
                       dict(e=eid))
            for m in mapeadas:
                cn.execute(text(f"UPDATE {m.__tablename__} SET empresa_id = :e WHERE empresa_id IS NULL"), dict(e=eid))
        for t in ("cz_ventas", "cz_ajustes", "cz_nomina"):
            cn.execute(text(f"UPDATE {t} SET consecutivo = id WHERE consecutivo IS NULL"))


def inicializar_bd():
    with app.app_context():
        try:
            db.create_all()
            migrar_columnas()
        except Exception as e:
            print("=" * 70, flush=True)
            print("ERROR: no se pudo conectar a la base de datos (DATABASE_URL).", flush=True)
            print("Revisa: usar 'Session pooler' (puerto 5432), contraseña correcta sin", flush=True)
            print("corchetes, y sin espacios. Detalle técnico:", flush=True)
            print(str(e).split("\n")[0][:300], flush=True)
            print("=" * 70, flush=True)
            raise
        # Usuario SoftGest (superadmin): administra las empresas desde /softgest
        if not Usuario.query.filter_by(rol="superadmin").first():
            try:
                user = os.getenv("SUPERADMIN_USER", "softgest").lower()
                if Usuario.query.filter(func.lower(Usuario.username) == user).first():
                    user += ".sg"
                sg = Usuario(nombre="SoftGest", username=user, rol="superadmin")
                clave = os.getenv("SUPERADMIN_PASSWORD") or os.getenv("ADMIN_PASSWORD")
                if not clave:
                    clave = secrets.token_urlsafe(9)
                    print(f"AVISO: no se definió SUPERADMIN_PASSWORD. Clave temporal de «{user}»: {clave}", flush=True)
                sg.set_password(clave)
                db.session.add(sg)
                db.session.commit()
            except Exception:
                db.session.rollback()
        # Las demos que quedaron "cargando" por un reinicio se liberan
        Empresa.query.filter_by(demo_cargando=True).update({"demo_cargando": False})
        db.session.commit()


inicializar_bd()

if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", 5000)))
