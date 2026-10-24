# CalzadoControl

App web para **fábricas y talleres de calzado**: materiales, producción por tallas y procesos, nómina por destajo, ventas con recibo, cartera a crédito y reportes. Hecha por SoftGest sobre la base de VikingoControl.

## Módulos

| Módulo | Qué resuelve |
|---|---|
| **Materiales** | Cueros (pie²/dm²), sintéticos y forros (metro), suelas y refuerzos (par/docena), pegantes (galón/ml), hilos, herrajes y empaques, con categoría, costo promedio ponderado y alerta de mínimo |
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

> Los recibos son soporte de la venta; **no reemplazan la factura electrónica DIAN**. La nómina es una ayuda de control; la planilla PILA y la liquidación legal las valida el contador.

## Desplegar en Render + Supabase

1. **Supabase** → proyecto → *Project Settings → Database → Connection string → URI* (Session pooler, puerto 5432), con la contraseña real.
2. **Render** → *New → Blueprint* → elige este repo (lee `render.yaml`).
3. Variables: `DATABASE_URL` (Supabase) y `ADMIN_PASSWORD`. `SECRET_KEY` se genera sola.
4. Al arrancar se crean las tablas (prefijo `cz_`, conviven con otras apps en la misma base) y el usuario `admin`.

Datos de prueba (solo con la base vacía): Render → *Shell* → `python seed_demo.py` → 21 materiales, 5 referencias, 7 empleados, 8 clientes y 60 días de producción, ventas, cartera, gastos y nómina. Usuarios `marta` (producción) y `julian` (vendedor), clave `demo123`.

## Estructura

```
app.py            rutas
models.py         tablas (cz_*)
services.py       compras, producción por tallas, ventas, abonos, devoluciones, ajustes
nomina.py         liquidación de nómina
comprobantes.py   recibo PDF/JPG, egreso y desprendible de nómina
reportes.py       reportes y Excel
seed_demo.py      datos de ejemplo
templates/ static/
```
