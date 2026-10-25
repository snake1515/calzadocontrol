"""Modelos de datos de CalzadoControl.

Todas las tablas usan el prefijo cz_ para poder convivir con otras apps
en la misma base de datos de Supabase.
"""
import contextvars
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event, func
from sqlalchemy.orm import Session, declared_attr, with_loader_criteria
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()
TZ = ZoneInfo("America/Bogota")

# ------------------------------------------------------------ multiempresa
# Cada fila de negocio pertenece a una empresa (empresa_id). Todas las consultas se
# filtran solas por la empresa activa y las filas nuevas la reciben al guardarse.
_empresa_actual = contextvars.ContextVar("empresa_actual", default=None)


def set_empresa(empresa_id):
    _empresa_actual.set(empresa_id)


def empresa_id_actual():
    return _empresa_actual.get()


class EmpresaMixin:
    @declared_attr
    def empresa_id(cls):
        return db.Column(db.Integer, db.ForeignKey("cz_empresas.id"), index=True)


@event.listens_for(Session, "do_orm_execute")
def _filtrar_por_empresa(st):
    if (st.is_select and not st.is_column_load and not st.is_relationship_load
            and not st.execution_options.get("todas_empresas")):
        eid = _empresa_actual.get()
        st.statement = st.statement.options(
            with_loader_criteria(EmpresaMixin, lambda cls: cls.empresa_id == eid, include_aliases=True))


@event.listens_for(Session, "before_flush")
def _asignar_empresa(session, ctx, inst):
    eid = _empresa_actual.get()
    siguientes = {}
    with session.no_autoflush:
        for obj in session.new:
            if isinstance(obj, EmpresaMixin) and obj.empresa_id is None:
                if eid is None:
                    raise RuntimeError(f"No hay empresa activa para guardar {type(obj).__name__}")
                obj.empresa_id = eid
            # Consecutivo propio de cada empresa (ventas, egresos, nómina)
            if hasattr(type(obj), "consecutivo") and getattr(obj, "consecutivo", 1) is None:
                cls = type(obj)
                if cls not in siguientes:
                    siguientes[cls] = (session.query(func.max(cls.consecutivo))
                                       .filter(cls.empresa_id == obj.empresa_id)
                                       .execution_options(todas_empresas=True).scalar() or 0)
                siguientes[cls] += 1
                obj.consecutivo = siguientes[cls]


def ahora():
    """Fecha y hora actual de Colombia (sin zona, para guardar en BD)."""
    return datetime.now(TZ).replace(tzinfo=None, microsecond=0)


ROLES = {
    "admin": "Administrador",
    "produccion": "Producción",
    "vendedor": "Vendedor",
}

UNIDADES = [
    "unidad", "par", "docena", "metro cuadrado", "decímetro cuadrado", "pie cuadrado",
    "metro", "centímetro", "yarda", "kilogramo", "gramo", "libra", "litro", "mililitro",
    "galón", "rollo", "lámina", "cono", "paquete", "caja", "bolsa",
]

CATEGORIAS_INSUMO = [
    "Cueros y sintéticos", "Forros", "Suelas y tacones", "Plantillas y refuerzos",
    "Pegantes y químicos", "Hilos", "Herrajes y accesorios", "Empaque", "Otros",
]

LINEAS = ["Dama", "Caballero", "Niño", "Niña", "Unisex", "Escolar", "Deportivo",
          "Dotación / seguridad"]

PROCESOS = ["Corte", "Guarnición (aparado)", "Montaje", "Soladura", "Terminado / alistado", "Otro"]

MODOS_MO = {
    "par": "Por par",
    "docena": "Por docena",
    "jornada": "Valor fijo / jornada",
}

TIPOS_PAGO = {
    "destajo": "Destajo (por par / docena)",
    "fijo": "Salario fijo mensual",
    "mixto": "Salario fijo + destajo",
}

METODOS_PAGO = ["Efectivo", "Nequi", "Daviplata", "Transferencia", "Tarjeta"]

TIPOS_CLIENTE = ["Almacén / tienda", "Distribuidor", "Particular", "Vendedor por catálogo",
                 "Empresa (dotación)", "Otro"]


ESTADOS_EMPRESA = {"demo": "Demo", "activa": "Activa", "suspendida": "Suspendida"}


class Empresa(db.Model):
    """Cliente de SoftGest que usa la app. Sus datos están aislados de los demás."""
    __tablename__ = "cz_empresas"
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False)
    slug = db.Column(db.String(40), unique=True, nullable=False)
    contacto = db.Column(db.String(120))
    telefono = db.Column(db.String(40))
    estado = db.Column(db.String(20), nullable=False, default="demo")
    vence = db.Column(db.Date)  # fin de la demo o de la suscripción; vacío = sin vencimiento
    nota = db.Column(db.String(255))
    demo_cargando = db.Column(db.Boolean, nullable=False, default=False)
    creado = db.Column(db.DateTime, default=ahora)

    @property
    def dias_restantes(self):
        if not self.vence:
            return None
        return (self.vence - ahora().date()).days

    @property
    def bloqueada(self):
        if self.estado == "suspendida":
            return True
        d = self.dias_restantes
        return d is not None and d < 0

    @property
    def estado_txt(self):
        if self.estado == "suspendida":
            return ("Suspendida", "err")
        d = self.dias_restantes
        if d is not None and d < 0:
            return ("Vencida", "err")
        if self.estado == "demo":
            return (f"Demo · {d} día{'s' if d != 1 else ''}" if d is not None else "Demo", "warn" if (d or 0) <= 5 else "")
        return ("Activa" + (f" · vence {self.vence.strftime('%d/%m/%Y')}" if self.vence else ""), "ok")


class Usuario(UserMixin, db.Model):
    __tablename__ = "cz_usuarios"
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey("cz_empresas.id"), index=True)  # vacío = SoftGest
    nombre = db.Column(db.String(120), nullable=False)
    username = db.Column(db.String(60), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    rol = db.Column(db.String(20), nullable=False, default="vendedor")
    activo = db.Column(db.Boolean, nullable=False, default=True)
    creado = db.Column(db.DateTime, default=ahora)
    intentos_fallidos = db.Column(db.Integer, nullable=False, default=0)
    bloqueado_hasta = db.Column(db.DateTime)  # tras 5 claves erradas seguidas, 15 minutos

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_active(self):
        return self.activo

    empresa = db.relationship("Empresa", lazy="joined")

    @property
    def rol_nombre(self):
        return "SoftGest (soporte)" if self.rol == "superadmin" else ROLES.get(self.rol, self.rol)

    @property
    def rol_efectivo(self):
        """El superadmin trabaja como administrador cuando entra a una empresa."""
        return "admin" if self.rol == "superadmin" else self.rol


class Configuracion(EmpresaMixin, db.Model):
    """Una sola fila: regla de distribución del dinero y parámetros de nómina."""
    __tablename__ = "cz_configuracion"
    id = db.Column(db.Integer, primary_key=True)
    pct_reinversion = db.Column(db.Float, nullable=False, default=60)
    pct_dueno = db.Column(db.Float, nullable=False, default=30)
    pct_emergencia = db.Column(db.Float, nullable=False, default=10)
    base = db.Column(db.String(20), nullable=False, default="ventas")  # ventas | ganancia
    # Nómina (valores 2026; se pueden cambiar cada año)
    smmlv = db.Column(db.Float, nullable=False, default=1750905)
    aux_transporte = db.Column(db.Float, nullable=False, default=249095)
    nom_aportes = db.Column(db.Boolean, nullable=False, default=True)
    nom_salud_empleador = db.Column(db.Boolean, nullable=False, default=False)
    nom_arl_pct = db.Column(db.Float, nullable=False, default=0.522)
    nom_prestaciones = db.Column(db.Boolean, nullable=False, default=True)

    @classmethod
    def actual(cls):
        c = cls.query.first()
        if not c:
            c = cls()
            db.session.add(c)
            db.session.commit()
        return c


class DatosNegocio(EmpresaMixin, db.Model):
    """Datos que salen en el encabezado de los recibos y facturas."""
    __tablename__ = "cz_negocio"
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False, default="Mi fábrica de calzado")
    nit = db.Column(db.String(40))
    direccion = db.Column(db.String(160))
    ciudad = db.Column(db.String(80))
    telefono = db.Column(db.String(40))
    email = db.Column(db.String(120))
    regimen = db.Column(db.String(120), default="No responsable de IVA")
    titulo_doc = db.Column(db.String(60), default="Recibo de venta")
    prefijo = db.Column(db.String(8), default="CZ")
    iva_pct = db.Column(db.Float, nullable=False, default=0)
    mensaje = db.Column(db.String(200), default="¡Gracias por su compra!")

    logo = db.Column(db.LargeBinary)  # JPG ya redimensionado
    logo_v = db.Column(db.Integer, nullable=False, default=0)

    @classmethod
    def actual(cls):
        d = cls.query.first()
        if not d:
            d = cls()
            db.session.add(d)
            db.session.commit()
        return d


# ------------------------------------------------------------------ insumos
class Insumo(EmpresaMixin, db.Model):
    __tablename__ = "cz_insumos"
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False)
    categoria = db.Column(db.String(40), default="Otros")
    unidad = db.Column(db.String(30), nullable=False, default="unidad")
    stock = db.Column(db.Float, nullable=False, default=0)
    costo_promedio = db.Column(db.Float, nullable=False, default=0)
    stock_minimo = db.Column(db.Float, nullable=False, default=0)
    activo = db.Column(db.Boolean, nullable=False, default=True)
    # Semielaborado propio (ej. plantilla armada): no se compra, se elabora con otros insumos
    es_preparacion = db.Column(db.Boolean, nullable=False, default=False)
    tanda_rinde = db.Column(db.Float)

    receta_prep = db.relationship(
        "PrepRecetaItem", foreign_keys="PrepRecetaItem.preparacion_id", lazy="selectin",
        cascade="all, delete-orphan", order_by="PrepRecetaItem.id",
        back_populates="preparacion")

    @property
    def costo_receta_prep(self):
        return sum(r.cantidad * (r.componente.costo_promedio or 0) for r in self.receta_prep)

    @property
    def valor_stock(self):
        return max(self.stock or 0, 0) * (self.costo_promedio or 0)

    @property
    def bajo_minimo(self):
        return (self.stock_minimo or 0) > 0 and (self.stock or 0) <= self.stock_minimo


class CompraInsumo(EmpresaMixin, db.Model):
    __tablename__ = "cz_compras"
    id = db.Column(db.Integer, primary_key=True)
    fecha = db.Column(db.DateTime, nullable=False, default=ahora, index=True)
    insumo_id = db.Column(db.Integer, db.ForeignKey("cz_insumos.id"), nullable=False)
    cantidad = db.Column(db.Float, nullable=False)
    costo_total = db.Column(db.Float, nullable=False)
    costo_unitario = db.Column(db.Float, nullable=False)
    proveedor = db.Column(db.String(120))
    nota = db.Column(db.String(255))
    usuario_id = db.Column(db.Integer, db.ForeignKey("cz_usuarios.id"))
    anulada = db.Column(db.Boolean, nullable=False, default=False)

    insumo = db.relationship("Insumo", lazy="joined")
    usuario = db.relationship("Usuario", lazy="joined")


# -------------------------------------------------------------- referencias
class Producto(EmpresaMixin, db.Model):
    """Una referencia de calzado (modelo + color). El stock se lleva por talla."""
    __tablename__ = "cz_productos"
    id = db.Column(db.Integer, primary_key=True)
    codigo = db.Column(db.String(30))
    nombre = db.Column(db.String(120), nullable=False)
    linea = db.Column(db.String(40), default="Dama")
    color = db.Column(db.String(60))
    material = db.Column(db.String(80))
    descripcion = db.Column(db.String(255))
    talla_min = db.Column(db.Integer, nullable=False, default=35)
    talla_max = db.Column(db.Integer, nullable=False, default=40)
    precio_venta = db.Column(db.Float, nullable=False, default=0)  # al detal, por par
    precio_mayor = db.Column(db.Float, nullable=False, default=0)  # al por mayor, por par
    stock = db.Column(db.Integer, nullable=False, default=0)  # total de pares (suma de tallas)
    costo_promedio = db.Column(db.Float, nullable=False, default=0)
    stock_minimo = db.Column(db.Integer, nullable=False, default=0)
    activo = db.Column(db.Boolean, nullable=False, default=True)
    tanda_rinde = db.Column(db.Float)  # pares que rinde la receta tal como se escribió

    receta = db.relationship(
        "RecetaItem", back_populates="producto", lazy="selectin",
        cascade="all, delete-orphan", order_by="RecetaItem.id",
    )
    procesos = db.relationship(
        "ProcesoTarifa", back_populates="producto", lazy="selectin",
        cascade="all, delete-orphan", order_by="ProcesoTarifa.id",
    )
    tallas_stock = db.relationship(
        "ProductoTalla", back_populates="producto", lazy="selectin",
        cascade="all, delete-orphan", order_by="ProductoTalla.talla",
    )

    @property
    def etiqueta(self):
        partes = [self.nombre]
        if self.color:
            partes.append(self.color)
        txt = " · ".join(partes)
        return f"{self.codigo} {txt}" if self.codigo else txt

    @property
    def rango_tallas(self):
        lo, hi = self.talla_min or 0, self.talla_max or 0
        if hi < lo:
            lo, hi = hi, lo
        return list(range(lo, hi + 1))

    @property
    def stock_por_talla(self):
        """dict talla -> pares, con todas las tallas del rango (y las que tengan stock fuera de él)."""
        d = {t: 0 for t in self.rango_tallas}
        for pt in self.tallas_stock:
            if pt.stock or pt.talla in d:
                d[pt.talla] = pt.stock or 0
        return dict(sorted(d.items()))

    def stock_talla(self, talla):
        for pt in self.tallas_stock:
            if pt.talla == talla:
                return pt.stock or 0
        return 0

    @property
    def costo_receta(self):
        """Costo solo de materiales para 1 par, con los costos promedio actuales."""
        return sum(r.cantidad * (r.insumo.costo_promedio or 0) for r in self.receta)

    @property
    def costo_mo_estandar(self):
        """Mano de obra estándar por par (tarifas de destajo por proceso)."""
        return sum(p.valor_par for p in self.procesos)

    @property
    def costo_estandar(self):
        return self.costo_receta + self.costo_mo_estandar

    @property
    def costo_ref(self):
        """Costo real si ya se produjo; si no, el estándar (materiales + mano de obra)."""
        return self.costo_promedio or self.costo_estandar

    @property
    def capacidad(self):
        """Cuántos pares se pueden hacer con el inventario actual de materiales.

        Devuelve (cantidad, insumo_limitante) o (None, None) si no hay receta.
        """
        mejor, limitante = None, None
        for r in self.receta:
            if r.cantidad <= 0:
                continue
            posible = int(max(r.insumo.stock or 0, 0) // r.cantidad)
            if mejor is None or posible < mejor:
                mejor, limitante = posible, r.insumo
        return mejor, limitante

    @property
    def margen_unitario(self):
        return (self.precio_venta or 0) - (self.costo_promedio or 0)

    @property
    def bajo_minimo(self):
        return (self.stock_minimo or 0) > 0 and (self.stock or 0) <= self.stock_minimo


class ProductoTalla(EmpresaMixin, db.Model):
    """Existencias de una referencia en una talla."""
    __tablename__ = "cz_producto_tallas"
    id = db.Column(db.Integer, primary_key=True)
    producto_id = db.Column(db.Integer, db.ForeignKey("cz_productos.id"), nullable=False, index=True)
    talla = db.Column(db.Integer, nullable=False)
    stock = db.Column(db.Integer, nullable=False, default=0)

    producto = db.relationship("Producto", back_populates="tallas_stock")


class RecetaItem(EmpresaMixin, db.Model):
    """Cantidad de un material que lleva 1 par (ficha técnica de consumo)."""
    __tablename__ = "cz_receta"
    id = db.Column(db.Integer, primary_key=True)
    producto_id = db.Column(db.Integer, db.ForeignKey("cz_productos.id"), nullable=False)
    insumo_id = db.Column(db.Integer, db.ForeignKey("cz_insumos.id"), nullable=False)
    cantidad = db.Column(db.Float, nullable=False)  # por 1 par, en la unidad del insumo
    unidad = db.Column(db.String(30))  # unidad en que se escribió, solo para mostrar

    producto = db.relationship("Producto", back_populates="receta")
    insumo = db.relationship("Insumo", lazy="joined")


class ProcesoTarifa(EmpresaMixin, db.Model):
    """Tarifa de destajo de un proceso para una referencia (ej. Guarnición: $3.500 por par)."""
    __tablename__ = "cz_producto_procesos"
    id = db.Column(db.Integer, primary_key=True)
    producto_id = db.Column(db.Integer, db.ForeignKey("cz_productos.id"), nullable=False)
    proceso = db.Column(db.String(40), nullable=False)
    valor_par = db.Column(db.Float, nullable=False, default=0)

    producto = db.relationship("Producto", back_populates="procesos")


# --------------------------------------------------------------- empleados
class Empleado(EmpresaMixin, db.Model):
    __tablename__ = "cz_empleados"
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False)
    documento = db.Column(db.String(40))
    telefono = db.Column(db.String(40))
    cargo = db.Column(db.String(80))
    proceso = db.Column(db.String(40))  # proceso que hace normalmente
    tipo_pago = db.Column(db.String(20), nullable=False, default="destajo")
    salario = db.Column(db.Float, nullable=False, default=0)  # mensual (fijo o mixto)
    aux_transporte = db.Column(db.Boolean, nullable=False, default=True)
    fecha_ingreso = db.Column(db.Date)
    activo = db.Column(db.Boolean, nullable=False, default=True)
    nota = db.Column(db.String(255))

    @property
    def tipo_nombre(self):
        return TIPOS_PAGO.get(self.tipo_pago, self.tipo_pago)

    @property
    def whatsapp(self):
        dig = "".join(ch for ch in (self.telefono or "") if ch.isdigit())
        if not dig:
            return None
        if len(dig) == 10 and dig.startswith("3"):
            dig = "57" + dig
        return f"https://wa.me/{dig}"


# --------------------------------------------------------------- producción
class Produccion(EmpresaMixin, db.Model):
    """Lote terminado: materiales + mano de obra por procesos + indirectos -> pares por talla."""
    __tablename__ = "cz_producciones"
    id = db.Column(db.Integer, primary_key=True)
    fecha = db.Column(db.DateTime, nullable=False, default=ahora, index=True)
    producto_id = db.Column(db.Integer, db.ForeignKey("cz_productos.id"), nullable=False)
    cantidad = db.Column(db.Integer, nullable=False)  # pares

    costo_insumos = db.Column(db.Float, nullable=False, default=0)
    costo_mano_obra = db.Column(db.Float, nullable=False, default=0)

    costo_energia = db.Column(db.Float, nullable=False, default=0)
    costo_gas_agua = db.Column(db.Float, nullable=False, default=0)
    costo_transporte = db.Column(db.Float, nullable=False, default=0)
    costo_otros = db.Column(db.Float, nullable=False, default=0)
    otros_descripcion = db.Column(db.String(255))

    costo_total = db.Column(db.Float, nullable=False, default=0)
    costo_unitario = db.Column(db.Float, nullable=False, default=0)
    nota = db.Column(db.String(255))
    usuario_id = db.Column(db.Integer, db.ForeignKey("cz_usuarios.id"))
    anulada = db.Column(db.Boolean, nullable=False, default=False)

    producto = db.relationship("Producto", lazy="joined")
    usuario = db.relationship("Usuario", lazy="joined")
    consumos = db.relationship(
        "ProduccionConsumo", back_populates="produccion", lazy="selectin",
        cascade="all, delete-orphan",
    )
    tallas = db.relationship(
        "ProduccionTalla", back_populates="produccion", lazy="selectin",
        cascade="all, delete-orphan", order_by="ProduccionTalla.talla",
    )
    mano_obra = db.relationship(
        "ProduccionMO", back_populates="produccion", lazy="selectin",
        cascade="all, delete-orphan", order_by="ProduccionMO.id",
    )

    @property
    def costo_indirectos(self):
        return (self.costo_energia + self.costo_gas_agua
                + self.costo_transporte + self.costo_otros)

    @property
    def corrida_txt(self):
        return " · ".join(f"{t.talla}×{t.cantidad}" for t in self.tallas if t.cantidad)


class ProduccionTalla(EmpresaMixin, db.Model):
    """Corrida: cuántos pares salieron de cada talla."""
    __tablename__ = "cz_produccion_tallas"
    id = db.Column(db.Integer, primary_key=True)
    produccion_id = db.Column(db.Integer, db.ForeignKey("cz_producciones.id"), nullable=False, index=True)
    talla = db.Column(db.Integer, nullable=False)
    cantidad = db.Column(db.Integer, nullable=False)

    produccion = db.relationship("Produccion", back_populates="tallas")


class ProduccionConsumo(EmpresaMixin, db.Model):
    __tablename__ = "cz_produccion_consumos"
    id = db.Column(db.Integer, primary_key=True)
    produccion_id = db.Column(db.Integer, db.ForeignKey("cz_producciones.id"), nullable=False)
    insumo_id = db.Column(db.Integer, db.ForeignKey("cz_insumos.id"), nullable=False)
    cantidad = db.Column(db.Float, nullable=False)
    costo_unitario = db.Column(db.Float, nullable=False)
    costo_total = db.Column(db.Float, nullable=False)

    produccion = db.relationship("Produccion", back_populates="consumos")
    insumo = db.relationship("Insumo", lazy="joined")


class ProduccionMO(EmpresaMixin, db.Model):
    """Mano de obra de un proceso dentro de un lote (destajo o valor fijo)."""
    __tablename__ = "cz_produccion_mo"
    id = db.Column(db.Integer, primary_key=True)
    produccion_id = db.Column(db.Integer, db.ForeignKey("cz_producciones.id"), nullable=False, index=True)
    proceso = db.Column(db.String(40), nullable=False)
    empleado_id = db.Column(db.Integer, db.ForeignKey("cz_empleados.id"), index=True)
    responsable = db.Column(db.String(120))  # si no es empleado registrado (taller externo, etc.)
    modo = db.Column(db.String(20), nullable=False, default="par")
    valor = db.Column(db.Float, nullable=False, default=0)
    pares = db.Column(db.Integer, nullable=False, default=0)
    total = db.Column(db.Float, nullable=False, default=0)
    nomina_id = db.Column(db.Integer, db.ForeignKey("cz_nomina.id"), index=True)  # liquidado en nómina

    produccion = db.relationship("Produccion", back_populates="mano_obra")
    empleado = db.relationship("Empleado", lazy="joined")
    nomina = db.relationship("PagoNomina", foreign_keys=[nomina_id], viewonly=True)

    @property
    def quien(self):
        return self.empleado.nombre if self.empleado else (self.responsable or "—")

    @property
    def detalle(self):
        if self.modo == "par":
            return f"{self.pares} pares × ${self.valor:,.0f}".replace(",", ".")
        if self.modo == "docena":
            return f"{self.pares / 12:g} doc. × ${self.valor:,.0f}".replace(",", ".")
        return "Valor fijo"


# ------------------------------------------------------------------ nómina
class PagoNomina(EmpresaMixin, db.Model):
    """Liquidación de nómina de un empleado para un periodo."""
    __tablename__ = "cz_nomina"
    id = db.Column(db.Integer, primary_key=True)
    consecutivo = db.Column(db.Integer)
    empleado_id = db.Column(db.Integer, db.ForeignKey("cz_empleados.id"), nullable=False, index=True)
    desde = db.Column(db.Date, nullable=False)
    hasta = db.Column(db.Date, nullable=False)
    fecha_pago = db.Column(db.Date, nullable=False, index=True)
    dias = db.Column(db.Integer, nullable=False, default=30)
    basico = db.Column(db.Float, nullable=False, default=0)
    aux_transporte = db.Column(db.Float, nullable=False, default=0)
    destajo = db.Column(db.Float, nullable=False, default=0)
    horas_extra = db.Column(db.Float, nullable=False, default=0)
    bonificaciones = db.Column(db.Float, nullable=False, default=0)  # no salariales
    deduccion_salud = db.Column(db.Float, nullable=False, default=0)
    deduccion_pension = db.Column(db.Float, nullable=False, default=0)
    otras_deducciones = db.Column(db.Float, nullable=False, default=0)
    deducciones_detalle = db.Column(db.String(255))
    neto = db.Column(db.Float, nullable=False, default=0)
    aportes = db.Column(db.Float, nullable=False, default=0)  # empleador: pensión, ARL, caja, salud
    provisiones = db.Column(db.Float, nullable=False, default=0)  # cesantías, intereses, prima, vacaciones
    costo_empresa = db.Column(db.Float, nullable=False, default=0)
    gasto = db.Column(db.Float, nullable=False, default=0)  # costo_empresa − destajo (ya va al costo del par)
    metodo = db.Column(db.String(30), default="Efectivo")
    nota = db.Column(db.String(255))
    anulada = db.Column(db.Boolean, nullable=False, default=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey("cz_usuarios.id"))

    empleado = db.relationship("Empleado", lazy="joined")
    usuario = db.relationship("Usuario", lazy="joined")
    lineas_destajo = db.relationship("ProduccionMO", lazy="selectin")

    @property
    def numero(self):
        return f"NM-{(self.consecutivo or self.id):05d}"

    @property
    def devengado(self):
        return self.basico + self.aux_transporte + self.destajo + self.horas_extra + self.bonificaciones

    @property
    def deducciones(self):
        return self.deduccion_salud + self.deduccion_pension + self.otras_deducciones


# ------------------------------------------------------------------ ventas
class Venta(EmpresaMixin, db.Model):
    __tablename__ = "cz_ventas"
    id = db.Column(db.Integer, primary_key=True)
    consecutivo = db.Column(db.Integer)
    fecha = db.Column(db.DateTime, nullable=False, default=ahora, index=True)
    cliente = db.Column(db.String(120))  # nombre tal como quedó en la venta
    cliente_id = db.Column(db.Integer, db.ForeignKey("cz_clientes.id"), index=True)
    cliente_doc = db.Column(db.String(40))
    metodo_pago = db.Column(db.String(30), default="Efectivo")
    condicion = db.Column(db.String(10), nullable=False, default="contado")  # contado | credito
    vence = db.Column(db.Date)
    nota = db.Column(db.String(255))
    descuento = db.Column(db.Float, nullable=False, default=0)
    iva_pct = db.Column(db.Float, nullable=False, default=0)
    iva = db.Column(db.Float, nullable=False, default=0)
    total = db.Column(db.Float, nullable=False, default=0)  # venta neta sin IVA (después de descuento)
    costo_total = db.Column(db.Float, nullable=False, default=0)
    ganancia = db.Column(db.Float, nullable=False, default=0)
    anulada = db.Column(db.Boolean, nullable=False, default=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey("cz_usuarios.id"))

    usuario = db.relationship("Usuario", lazy="joined")
    items = db.relationship(
        "VentaItem", back_populates="venta", cascade="all, delete-orphan", lazy="selectin",
        order_by="VentaItem.id",
    )
    abonos = db.relationship("Abono", back_populates="venta", lazy="selectin",
                             cascade="all, delete-orphan", order_by="Abono.fecha")

    @property
    def unidades(self):
        return sum(i.cantidad for i in self.items)

    @property
    def subtotal(self):
        return sum(i.subtotal for i in self.items)

    @property
    def total_factura(self):
        return (self.total or 0) + (self.iva or 0)

    @property
    def pagado(self):
        if self.anulada:
            return 0
        if self.condicion != "credito":
            return self.total_factura
        return sum(a.valor for a in self.abonos if not a.anulado)

    @property
    def saldo(self):
        if self.anulada or self.condicion != "credito":
            return 0
        return max(self.total_factura - self.pagado, 0)

    @property
    def vencida(self):
        return bool(self.saldo > 0.5 and self.vence and self.vence < ahora().date())

    @property
    def estado_pago(self):
        if self.anulada:
            return ("Anulada", "err")
        if self.condicion != "credito" or self.saldo <= 0.5:
            return ("Pagada", "ok")
        if self.vencida:
            return (f"Vencida hace {(ahora().date() - self.vence).days} d", "err")
        return ("Pendiente", "warn")


class VentaItem(EmpresaMixin, db.Model):
    __tablename__ = "cz_venta_items"
    id = db.Column(db.Integer, primary_key=True)
    venta_id = db.Column(db.Integer, db.ForeignKey("cz_ventas.id"), nullable=False)
    producto_id = db.Column(db.Integer, db.ForeignKey("cz_productos.id"), nullable=False)
    talla = db.Column(db.Integer)
    cantidad = db.Column(db.Integer, nullable=False)
    precio_unitario = db.Column(db.Float, nullable=False)
    costo_unitario = db.Column(db.Float, nullable=False)
    subtotal = db.Column(db.Float, nullable=False)
    costo = db.Column(db.Float, nullable=False)

    venta = db.relationship("Venta", back_populates="items")
    producto = db.relationship("Producto", lazy="joined")


class Abono(EmpresaMixin, db.Model):
    """Pago parcial o total de una venta a crédito."""
    __tablename__ = "cz_abonos"
    id = db.Column(db.Integer, primary_key=True)
    venta_id = db.Column(db.Integer, db.ForeignKey("cz_ventas.id"), nullable=False, index=True)
    fecha = db.Column(db.DateTime, nullable=False, default=ahora, index=True)
    valor = db.Column(db.Float, nullable=False)
    metodo = db.Column(db.String(30), default="Efectivo")
    nota = db.Column(db.String(255))
    anulado = db.Column(db.Boolean, nullable=False, default=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey("cz_usuarios.id"))

    venta = db.relationship("Venta", back_populates="abonos")
    usuario = db.relationship("Usuario", lazy="joined")


# ------------------------------------------------------------------ activos
class Activo(EmpresaMixin, db.Model):
    """Máquinas y herramientas (guarnecedora, troqueladora, hormas…). Depreciación en línea recta."""
    __tablename__ = "cz_activos"
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False)
    descripcion = db.Column(db.String(255))
    fecha_compra = db.Column(db.Date, nullable=False)
    valor = db.Column(db.Float, nullable=False)
    valor_residual = db.Column(db.Float, nullable=False, default=0)
    vida_util_meses = db.Column(db.Integer, nullable=False)
    fecha_baja = db.Column(db.Date)
    motivo_baja = db.Column(db.String(255))
    eliminado = db.Column(db.Boolean, nullable=False, default=False)
    proveedor = db.Column(db.String(120))

    @property
    def depreciable(self):
        return max((self.valor or 0) - (self.valor_residual or 0), 0)

    @property
    def cuota_mensual(self):
        return self.depreciable / self.vida_util_meses if self.vida_util_meses else 0

    @property
    def cuota_diaria(self):
        return self.cuota_mensual * 12 / 365

    @property
    def fin_vida(self):
        return self.fecha_compra + timedelta(days=round(self.vida_util_meses * 365 / 12))

    def depreciacion_entre(self, desde, hasta):
        """Depreciación de los días [desde, hasta) — fechas date."""
        ini = max(desde, self.fecha_compra)
        fin = min(hasta, self.fin_vida, self.fecha_baja or hasta)
        dias = (fin - ini).days
        return max(dias, 0) * self.cuota_diaria

    def acumulada(self, hoy):
        return min(self.depreciacion_entre(self.fecha_compra, hoy + timedelta(days=1)), self.depreciable)

    def estado(self, hoy):
        if self.fecha_baja:
            return "baja"
        if hoy >= self.fin_vida:
            return "recuperado"
        return "vigente"


CATEGORIAS_GASTO = ["Arriendo", "Energía eléctrica", "Agua y alcantarillado", "Gas",
                    "Internet / celular", "Publicidad", "Transporte / fletes", "Maquila externa",
                    "Mantenimiento de máquinas", "Honorarios (contador, asesorías)",
                    "Impuestos y trámites", "Papelería y aseo", "Otros"]


class Gasto(EmpresaMixin, db.Model):
    """Gastos de la fábrica que no son materiales, nómina ni máquinas: arriendo, servicios…"""
    __tablename__ = "cz_gastos"
    id = db.Column(db.Integer, primary_key=True)
    fecha = db.Column(db.Date, nullable=False, index=True)
    categoria = db.Column(db.String(60), nullable=False, default="Otros")
    descripcion = db.Column(db.String(255))
    valor = db.Column(db.Float, nullable=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey("cz_usuarios.id"))
    usuario = db.relationship("Usuario")


# ----------------------------------------------------------- semielaborados
class PrepRecetaItem(EmpresaMixin, db.Model):
    """Cuánto de cada insumo lleva 1 unidad de un semielaborado (ej. 1 par de plantillas armadas)."""
    __tablename__ = "cz_prep_receta"
    id = db.Column(db.Integer, primary_key=True)
    preparacion_id = db.Column(db.Integer, db.ForeignKey("cz_insumos.id"), nullable=False)
    componente_id = db.Column(db.Integer, db.ForeignKey("cz_insumos.id"), nullable=False)
    cantidad = db.Column(db.Float, nullable=False)
    unidad = db.Column(db.String(30))

    preparacion = db.relationship("Insumo", foreign_keys=[preparacion_id], back_populates="receta_prep")
    componente = db.relationship("Insumo", foreign_keys=[componente_id], lazy="joined")


class Preparacion(EmpresaMixin, db.Model):
    """Elaboración de un semielaborado propio: insumos → plantillas armadas, cortes, etc."""
    __tablename__ = "cz_preparaciones"
    id = db.Column(db.Integer, primary_key=True)
    fecha = db.Column(db.DateTime, nullable=False, default=ahora, index=True)
    insumo_id = db.Column(db.Integer, db.ForeignKey("cz_insumos.id"), nullable=False)
    cantidad = db.Column(db.Float, nullable=False)
    costo_insumos = db.Column(db.Float, nullable=False, default=0)
    costo_mano_obra = db.Column(db.Float, nullable=False, default=0)
    costo_otros = db.Column(db.Float, nullable=False, default=0)
    otros_descripcion = db.Column(db.String(255))
    costo_total = db.Column(db.Float, nullable=False, default=0)
    costo_unitario = db.Column(db.Float, nullable=False, default=0)
    nota = db.Column(db.String(255))
    anulada = db.Column(db.Boolean, nullable=False, default=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey("cz_usuarios.id"))

    insumo = db.relationship("Insumo", lazy="joined")
    usuario = db.relationship("Usuario", lazy="joined")
    consumos = db.relationship("PreparacionConsumo", back_populates="preparacion", lazy="selectin",
                               cascade="all, delete-orphan")


class PreparacionConsumo(EmpresaMixin, db.Model):
    __tablename__ = "cz_preparacion_consumos"
    id = db.Column(db.Integer, primary_key=True)
    preparacion_id = db.Column(db.Integer, db.ForeignKey("cz_preparaciones.id"), nullable=False)
    insumo_id = db.Column(db.Integer, db.ForeignKey("cz_insumos.id"), nullable=False)
    cantidad = db.Column(db.Float, nullable=False)
    costo_unitario = db.Column(db.Float, nullable=False)
    costo_total = db.Column(db.Float, nullable=False)

    preparacion = db.relationship("Preparacion", back_populates="consumos")
    insumo = db.relationship("Insumo", lazy="joined")


# ----------------------------------------------------------------- clientes
class Cliente(EmpresaMixin, db.Model):
    __tablename__ = "cz_clientes"
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(120), nullable=False)
    tipo = db.Column(db.String(30), default="Almacén / tienda")
    identificacion = db.Column(db.String(40))  # cédula o NIT
    telefono = db.Column(db.String(40))
    email = db.Column(db.String(120))
    direccion = db.Column(db.String(160))
    ciudad = db.Column(db.String(80))
    plazo_dias = db.Column(db.Integer, nullable=False, default=0)  # crédito habitual
    cupo = db.Column(db.Float)  # cupo de crédito
    usa_precio_mayor = db.Column(db.Boolean, nullable=False, default=False)
    nota = db.Column(db.String(255))
    activo = db.Column(db.Boolean, nullable=False, default=True)
    creado = db.Column(db.DateTime, default=ahora)

    @property
    def whatsapp(self):
        dig = "".join(ch for ch in (self.telefono or "") if ch.isdigit())
        if not dig:
            return None
        if len(dig) == 10 and dig.startswith("3"):
            dig = "57" + dig
        return f"https://wa.me/{dig}"


class Devolucion(EmpresaMixin, db.Model):
    """Devolución parcial de una venta (cambio, defecto, el cliente no recibió todo)."""
    __tablename__ = "cz_devoluciones"
    id = db.Column(db.Integer, primary_key=True)
    venta_id = db.Column(db.Integer, db.ForeignKey("cz_ventas.id"), nullable=False, index=True)
    fecha = db.Column(db.DateTime, nullable=False, default=ahora)
    motivo = db.Column(db.String(255))
    total = db.Column(db.Float, nullable=False, default=0)
    unidades = db.Column(db.Integer, nullable=False, default=0)
    usuario_id = db.Column(db.Integer, db.ForeignKey("cz_usuarios.id"))
    usuario = db.relationship("Usuario", lazy="joined")
    items = db.relationship("DevolucionItem", lazy="selectin", cascade="all, delete-orphan")


class DevolucionItem(EmpresaMixin, db.Model):
    __tablename__ = "cz_devolucion_items"
    id = db.Column(db.Integer, primary_key=True)
    devolucion_id = db.Column(db.Integer, db.ForeignKey("cz_devoluciones.id"), nullable=False)
    producto_id = db.Column(db.Integer, db.ForeignKey("cz_productos.id"), nullable=False)
    talla = db.Column(db.Integer)
    cantidad = db.Column(db.Integer, nullable=False)
    precio_unitario = db.Column(db.Float, nullable=False)
    producto = db.relationship("Producto", lazy="joined")


MOTIVOS_AJUSTE = ["Ajuste por conteo físico", "Pérdida", "Daño o defecto", "Muestra / exhibición",
                  "Otro"]


class Ajuste(EmpresaMixin, db.Model):
    """Documento de egreso: saca materiales o pares de la bodega (pérdida, conteo, defecto…)."""
    __tablename__ = "cz_ajustes"
    id = db.Column(db.Integer, primary_key=True)
    consecutivo = db.Column(db.Integer)
    fecha = db.Column(db.DateTime, nullable=False, default=ahora, index=True)
    motivo = db.Column(db.String(40), nullable=False, default="Pérdida")
    descripcion = db.Column(db.String(255))
    total = db.Column(db.Float, nullable=False, default=0)
    anulada = db.Column(db.Boolean, nullable=False, default=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey("cz_usuarios.id"))
    usuario = db.relationship("Usuario", lazy="joined")
    items = db.relationship("AjusteItem", back_populates="ajuste", lazy="selectin",
                            cascade="all, delete-orphan", order_by="AjusteItem.id")

    @property
    def numero(self):
        return f"EG-{(self.consecutivo or self.id):06d}"


class AjusteItem(EmpresaMixin, db.Model):
    __tablename__ = "cz_ajuste_items"
    id = db.Column(db.Integer, primary_key=True)
    ajuste_id = db.Column(db.Integer, db.ForeignKey("cz_ajustes.id"), nullable=False, index=True)
    insumo_id = db.Column(db.Integer, db.ForeignKey("cz_insumos.id"))
    producto_id = db.Column(db.Integer, db.ForeignKey("cz_productos.id"))
    talla = db.Column(db.Integer)
    cantidad = db.Column(db.Float, nullable=False)
    stock_antes = db.Column(db.Float)
    costo_unitario = db.Column(db.Float, nullable=False, default=0)
    costo_total = db.Column(db.Float, nullable=False, default=0)

    ajuste = db.relationship("Ajuste", back_populates="items")
    insumo = db.relationship("Insumo", lazy="joined")
    producto = db.relationship("Producto", lazy="joined")

    @property
    def item(self):
        return self.insumo or self.producto

    @property
    def nombre(self):
        if self.insumo:
            return self.insumo.nombre
        if self.producto:
            return self.producto.etiqueta + (f" · T{self.talla}" if self.talla else "")
        return "—"

    @property
    def unidad(self):
        return self.insumo.unidad if self.insumo else "pares"
