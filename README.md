# CalzadoControl

App web **multiempresa** para **fábricas y talleres de calzado**: materiales, producción por tallas y procesos, nómina por destajo, ventas con recibo, cartera a crédito y reportes. Hecha por SoftGest sobre la base de VikingoControl.

## Módulos

| Módulo | Qué resuelve |
|---|---|
| **Materiales** | Cueros (m²; en la ficha se puede escribir en dm²), sintéticos y forros (metro), suelas y refuerzos (par/docena), pegantes (galón/ml), hilos, herrajes y empaques, con categoría, costo promedio ponderado y alerta de mínimo |
| **Compras** | Entrada de materiales (varios en una compra) y de máquinas/herramientas |
| **Semielaborados** | Plantillas armadas, contrafuertes, cortes por tarea: se elaboran con materiales y quedan con su costo real |
| **Referencias** | Modelo + color, línea (dama, caballero, escolar…), rango de tallas, precio detal y por mayor, **ficha de consumo** por par y **tarifas de destajo por proceso**. Stock **por talla**. Botón para crear otro color con la misma ficha |
| **Producción** | Lote terminado con su **corrida** (pares por talla), materiales usados (llenados por la ficha), **mano de obra por proceso** (corte, guarnición, montaje, soladura, terminado) asignada a cada empleado o taller externo (por par, docena o valor fijo) e indirectos → costo real por par |
| **Empleados y nómina** | Destajo, sueldo fijo o mixto. La nómina suma sola el destajo pendiente del periodo, salario proporcional, auxilio de transporte, horas extra, deducciones (salud, pensión, anticipos), aportes del empleador y provisión de prestaciones. Desprendible en PDF |
| **Ventas** | Toque por talla, lista de precios detal/mayor, descuento, IVA opcional, **contado o crédito** con plazo y abono inicial. Recibo en **PDF y JPG** (WhatsApp) |
| **Cartera** | Saldos por cobrar, vencidos, edad de la cartera, abonos y recordatorio por WhatsApp |
| **Clientes** | Almacenes, distribuidores, catálogo, dotación; plazo, cupo, precio por mayor, frecuencia de compra y saldo |
| **Gastos y servicios** | Arriendo, energía, agua, gas, internet, fletes, contador… |
| **Máquinas** | Guarnecedoras, troqueladora, hormas… con depreciación en línea recta |
| **Resumen y reportes** | Ventas, costo, nómina, gastos, depreciación y **ganancia neta**; referencias y tallas más vendidas; Excel con consolidado, por referencia, por talla, por cliente, gastos, compras, ventas, nómina y cartera |
| **Distribución** | Regla de reparto del dinero (reinversión / dueño / emergencia) |
| **Usuarios** | Administrador, Producción y Vendedor |

### Cómo se calcula la ganancia
1. Cada compra actualiza el costo promedio del material.
2. Cada producción: `(materiales + mano de obra + indirectos) ÷ pares = costo por par`, promediado con lo que ya había.
3. Cada venta guarda ese costo → ganancia bruta = venta neta (sin IVA) − costo.
4. Ganancia neta = bruta − nómina (sueldos, aportes y prestaciones; el destajo ya va en el costo del par) − gastos − pérdidas − depreciación.

> Los recibos son soporte de la venta; **no reemplazan la factura electrónica DIAN**. La nómina es una ayuda de control; la planilla PILA y la liquidación legal las vali## Multiempresa y demos (panel SoftGest)

- Un solo despliegue atiende a todas las empresas. Cada empresa ve **solo sus datos** (filtro automático por `empresa_id` en todas las consultas) y tiene su propia numeración, logo, usuarios y configuración.
- El usuario **SoftGest** (`SUPERADMIN_USER`) entra a `/softgest` y desde ahí:
  - crea empresas en **demo** (15 días por defecto) o **activas**, con su usuario administrador;
  - carga **datos de ejemplo** (60 días de una fábrica) para mostrar la app;
  - **extiende** la demo, la **activa** sin vencimiento (pago único) o la **suspende**;
  - **entra como soporte** a cualquier empresa;
  - **vacía** los datos de una empresa para que arranque limpia al comprar.
- Durante la demo, la app muestra arriba **los días que quedan** (ámbar a 5 días, rojo a 2) con botón para activar por WhatsApp. Al vencer, **se bloquea** el ingreso y se muestra cómo activar; **los datos se conservan**.

## Desplegar en Render + Supabase

1. **Supabase** → *Connect* → **Session pooler** (puerto 5432), con la contraseña real. Puede ser la misma base de otras apps: las tablas usan el prefijo `cz_`.
2. **Render** → *New → Web Service* con este repo:
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn app:app --workers 2 --threads 4 --timeout 60`
3. Variables de entorno:
   - `DATABASE_URL` → la de Supabase
   - `SECRET_KEY` → texto largo aleatorio
   - `SUPERADMIN_USER` → `softgest` (usuario del panel)
   - `SUPERADMIN_PASSWORD` → clave del panel
   - `SOFTGEST_WHATSAPP` → tu número con 57 (ej. `573001234567`), para el botón «Activar»
   - `PYTHON_VERSION` → `3.12.4`
4. Entra con el usuario SoftGest → *Nueva empresa / demo*.

Si ya existía una instalación de una sola empresa, al arrancar se convierte sola en la empresa «Mi empresa» sin perder datos.

Por consola (opcional): `python demo.py "Nombre"` crea una empresa demo con datos de ejemplo.

lave `demo123`.

## Estructura

```
app.py            rutas
models.py         tablas (cz_*)
services.py       compras, producción por tallas, ventas, abonos, devoluciones, ajustes
nomina.py         liquidación de nómina
comprobantes.py   recibo PDF/JPG, egreso y desprendible de nómina
reportes.py       reportes y Excel
demo.py           datos de ejemplo por empresa
templates/ static/
```
