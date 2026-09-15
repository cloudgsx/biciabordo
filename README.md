# biciabordo

**En marcha en https://biciabordo.es** · AGPL-3.0

Cruzar España en tren **con la bici montada**. Sin desmontar, sin funda.

> El cuerpo de este README es de agosto y todavía llama `bicitren` al proyecto
> y al comando. El nombre cambió en septiembre de 2026; el código y el uso no.

Planificador punto-a-punto sobre los GTFS oficiales de Renfe que sólo propone
trenes en los que cabe una bici armada — Cercanías, Rodalies, ancho métrico
(ex-FEVE), Regional, Regional Exprés y Media Distancia — y que encadena varios
días cuando hace falta, porque Barcelona → Oviedo no se hace en una tarde.

```
$ bicitren plan "Barcelona" "Oviedo"

Barcelona-Sants → Oviedo   saliendo Tue 11 Aug 07:00

6 trenes · 38h01 · 4.9 km en bici · llega Wed 12 Aug 21:01 (2 días)
  07:00 🚲   3.7 km  Origen → Barcelona Estació de França
  08:45 🚆 REG.EXP.  Barcelona Estació de França → Zaragoza-Miraflores   [reserva]
  16:43 🚆 REG.EXP.  Zaragoza-Miraflores → Pamplona/Iruña                [reserva]
  19:54 🚆 REG.EXP.  Pamplona/Iruña → Altsasu-Pueblo                     [reserva]
  20:40 🚲   1.1 km  Altsasu-Pueblo → Altsasu
        🛏  noche en Altsasu (10h16)
  07:06 🚆 MD        Altsasu → Venta de Baños                            [reserva]
  10:27 🚆 REG.EXP.  Venta de Baños → Santander                          [reserva]
  15:28 🚆 REGIONAL  Santander → Oviedo                                  [reserva]
```

## Por qué existe

El [mapa de ConAlforjas](https://conalforjas.com/mapa-trenes/) enseña *dónde*
están las líneas donde cabe la bici, pero no resuelve la pregunta real: dado un
punto A y un punto B, ¿qué trenes concretos encadeno, a qué hora, y dónde
duermo? Eso es lo que hace esto.

## Cómo funciona

**Datos.** Renfe es la base y el valor por defecto; los operadores autonómicos se
activan con una casilla, porque llegan donde Renfe no llega
([`feeds.py`](app/gtfs/feeds.py)):

| feed | operador | contenido | descarga |
|---|---|---|---|
| `cer` | Renfe | Cercanías, Rodalies y ancho métrico (ex-FEVE) | automática |
| `avld` | Renfe | Alta velocidad, larga y media distancia | automática |
| `fgc` | FGC | Barcelona-Vallès, Llobregat-Anoia, **Lleida–La Pobla de Segur**, cremalleras | automática |
| `euskotren` | Euskotren | tren, tranvía y funicular del País Vasco | automática |
| `tram_alacant` | FGV | **TRAM d'Alacant**: Alacant, Benidorm, Dénia | **manual**, ver abajo |

Los dos feeds de Renfe comparten su espacio de códigos de estación (678 códigos
aparecen en ambos), así que se fusionan cruzando por `stop_id`. Los demás
operadores **no** comparten ese espacio, así que se les prefija el `stop_id` y el
enlace entre operadores se resuelve geográficamente: dos estaciones a menos de
150 m son la misma (Lleida-Pirineus de Renfe y Lleida de FGC, por ejemplo).

**El TRAM d'Alacant hay que bajarlo a mano.** El NAP lo publica tras iniciar
sesión, así que no se puede automatizar. Regístrate gratis en
[nap.transportes.gob.es](https://nap.transportes.gob.es/Files/Detail/966),
descarga el zip y déjalo en `data/feeds/tram_alacant.zip`; la ingesta lo recoge
si está. Sin él, Benidorm aparece como "sin estación".

**Política de bici.** Renfe **no publica** el campo `bikes_allowed` de GTFS, así
que la clasificación se deduce del nombre comercial del producto
([`bikepolicy.py`](app/gtfs/bikepolicy.py)), siguiendo la normativa de julio 2025:

| clase | productos | qué implica |
|---|---|---|
| `FREE` | Cercanías, Rodalies, ancho métrico | bici montada, gratis, sin reserva |
| `RESERVED` | MD, Regional, Reg. Exprés, Proximidad | bici montada + extra Tren+Bici (gratis), ~3 plazas/tren |
| `PARTIAL` | Avant | sólo en unidades ya adaptadas, no garantizado |
| `BAGGED` | AVE, Avlo, Alvia, Intercity, Euromed | desmontada y en funda — excluido por defecto |

Un producto desconocido se clasifica como `BAGGED`: es preferible ocultar un
tren que mandarte a un andén donde te van a dar la vuelta.

**Búsqueda.** RAPTOR ([`raptor.py`](app/routing/raptor.py)) con tres cambios:

- Los trenes por encima de la clase tolerada se saltan durante el escaneo, así
  que un AVE nunca aparece en un resultado.
- Los transbordos son **tramos en bici entre estaciones**, no sólo cambios de
  andén: pedalear 12 km hasta la línea de al lado es a menudo el único paso. Hay
  un presupuesto de kilómetros configurable.
- La línea de tiempo abarca varios días, así que una espera larga es legítima.
  Un enlace en bici que no cabe en horas de luz se aplaza a la mañana siguiente y
  se muestra como noche, en vez de mandarte por una N sin arcén a las 00:30.

**Sitios sin estación.** Berga, Cuenca y buena parte del interior perdieron la
línea hace décadas. En vez de un "sin combinaciones", el planificador propone las
estaciones a las que realmente irías, **ordenadas por esfuerzo, no por distancia**
([`elevation.py`](app/elevation.py)). Desde Berga:

| estación | distancia | ascenso | punto alto | en bici |
|---|---|---|---|---|
| Sant Quirze de Besora | 30,7 km | 468 m | 919 m | **3h42** |
| Manresa | 42,8 km | 120 m | 745 m | **3h59** |
| Toses | 27,8 km | 1.838 m | 1.936 m | 6h30 |
| La Molina | 28,1 km | 1.952 m | 2.142 m | 6h46 |

Las dos más cercanas con una regla son las dos peores: cruzan la Collada de Toses
a 1.800 m. El tiempo se modela como rodar en llano (15 km/h) más un ritmo
vertical (450 m/h de ascenso), que es discutible pero transparente.

Elevación vía Open-Elevation, con Open-Meteo y OpenTopoData como reserva
(`BICITREN_ELEVATION_PROVIDER` para forzar uno). Se cachea para siempre en
`data/elevation.sqlite` — el terreno no se mueve — así que la primera consulta
tarda ~2 s y las siguientes son instantáneas. Si ningún proveedor responde, se
vuelve al orden por distancia y **se dice explícitamente**, en vez de presentar un
puerto de montaña como si fuera llano.

**Obras y buses sustitutorios.** No existe una API nacional de cortes de línea,
pero el corte ya está en el horario si sabes dónde mirar: Renfe publica los
autobuses sustitutorios **dentro del propio GTFS de Cercanías**, como rutas
normales con el número de la línea, distinguidas sólo por `route_type=3`. Así que
una estación con autobuses y sin trenes ese día está, ese día, fuera de la red
para quien lleva bici ([`closures.py`](app/closures.py)).

```
$ bicitren closures

R3     Renfe Cercanías
  sin tren: Barcelona Fabra i Puig, Granollers-Canovelles, Les Franqueses del
            Vallès, Mollet-Santa Rosa, Montcada-Bifurcació, Montcada-Ripollet,
            Parets del Vallès, Santa Perpètua de Mogoda la Florida
  llega el tren hasta: La Garriga (18 km), Figaró (22 km)
```

Lo importante no es "la línea está cortada" sino **hasta dónde llega todavía el
tren**, porque el bus no admite bicis. Por eso cada corte informa de su cabecera.
Y el planificador lo aplica solo: Granollers → Vic te manda 8 km en bici hasta La
Garriga y coge el tren allí, además de avisarte del corte.

Renfe reutiliza los códigos de línea entre núcleos — hay una C3 en Santander y
otra en Valencia — así que los cortes se agrupan **geográficamente**, no por
nombre, o saldría una cabecera a 300 km en otra provincia.

**La mejor combinación del día.** Una consulta RAPTOR responde "si salgo a las t,
¿cuándo llego lo antes posible?", que es la pregunta equivocada cuando la hora de
salida es negociable. Barcelona → València saliendo a las 04:00 llega antes, pero
son 2 trenes y 34 km de bici; el de las 09:33 es directo. Con la casilla **mejor
combinación del día** se ignora la hora y se busca el trayecto más corto:

```
$ bicitren plan "Barcelona-Sants" "València-Estació del Nord" --best

1 tren · 5h45 · 3.7 km en bici · sale 09:33, llega 15:18
  09:33 🚆 REG.EXP.  Barcelona-Sants → València-Cabanyal   [reserva]
  14:59 🚲   3.7 km  València-Cabanyal → Destino
```

No barre el reloj minuto a minuto: lanza una consulta, salta justo después del
primer tren que ha usado, y repite. Así cada ronda cae forzosamente en una salida
posterior y el día entero sale en una docena de consultas (~1,5 s).

Con la casilla marcada, **la hora deja de ser fija y pasa a ser un mínimo**: pon
00:00 para el día entero, o 09:00 para descartar madrugones. Sólo se devuelven
salidas del día pedido; el tren de mañana es el plan de otro día.

Cuando el servicio es regular, lo útil es el horario entero, no un ganador. Por
eso el filtro de Pareto conserva los empates: descarta un trayecto sólo si otro lo
gana **estrictamente** en algo. Con dominancia débil, Barcelona → Manresa mostraba
1 salida en vez de las 17 que hay:

```
1 tren · 1h25 · sale 05:58, llega 07:23
1 tren · 1h25 · sale 06:58, llega 08:23
1 tren · 1h25 · sale 07:58, llega 09:23   ...
```

La hora de salida que se muestra es **la última con la que aún se coge el primer
tren**, no la de la consulta, así que la duración mide el viaje y no la espera en
el andén.

## Uso

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m app.gtfs.ingest        # descarga y construye (~10 s, 162 MB)
.venv/bin/python -m app.cli plan "Villanueva de la Serena" "Calera y Chozas"
.venv/bin/python -m flask --app app.api run --port 8022   # interfaz con mapa
```

Docker:

```bash
docker compose up -d      # construye la base al arrancar y la refresca a diario
```

Tests:

```bash
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests
```

## Limitaciones

- **Horizonte real ≈ 30 días.** Renfe publica Cercanías en ventana móvil (hasta
  2026-09-09 en el momento de escribir esto) y media distancia bastante más allá.
  Pasada esa fecha los enlaces de cercanías desaparecen sin avisar; la interfaz
  lo advierte, pero conviene saberlo.
- **Sin disponibilidad de plazas.** El GTFS son horarios, no inventario. Las
  ~3 plazas de bici por tren de Media Distancia se agotan; hay que comprobar en
  Renfe antes de fiarse.
- **Sin tiempo real.** No hay GTFS-RT público de Renfe. Incidencias y retrasos no
  se reflejan; los cortes se detectan del horario publicado, así que aparecen
  cuando Renfe los mete en el GTFS, no en el momento de la avería.
- **Los cortes dependen de que el operador marque el bus como `route_type=3`.**
  Renfe y FGC lo hacen. Un operador que publique el sustitutorio como si fuera
  tren pasaría desapercibido — y sería un fallo silencioso, del peor tipo.
- **Política de bici de los operadores autonómicos, deducida.** Ninguno publica
  reglas legibles por máquina, así que se clasifica por `route_type`: tren pesado
  admite bici montada, y tranvía, metro y cremallera se marcan como *no
  garantizado*. Las restricciones de hora punta de FGC y Euskotren no están
  modeladas, sólo advertidas en cada tramo.
- Faltan Metrovalencia (FGV), TRAM de Murcia, Metro de Bilbao y los tranvías
  urbanos. Encajan en el mismo registro si aparecen con descarga pública.
- **Geometría recta.** Los tramos se dibujan estación a estación en línea recta;
  el feed de cercanías trae `shapes.txt` y podría usarse para el trazado real.
- Los kilómetros en bici son distancia en línea recta × 1.30, no ruta real.
- El desnivel se muestrea **sobre la línea recta**, no sobre la carretera. Como
  las carreteras contornean y buscan collados, esto **sobreestima** el ascenso
  real: Berga→Toses da 1.838 m muestreando la recta, que cruza las crestas del
  Moixeró, mientras que la carretera por Bagà y la Collada de Toses sube bastante
  menos. Sirve para ordenar opciones, no como perfil de ruta.
- Los transbordos en bici entre estaciones **dentro** de un viaje siguen
  ordenándose sólo por distancia. Hacerlos sensibles al desnivel son ~58.000
  aristas, demasiado para una API pública: necesitaría un DEM local.

---

## Licencia

AGPL-3.0. Ver [LICENSE](LICENSE). Si despliegas una versión modificada como
servicio público, la licencia te obliga a ofrecer su código fuente.

Los datos no son míos: los GTFS son de Renfe, FGC y Euskotren bajo sus
respectivos términos; el callejero viene de [GeoNames](https://www.geonames.org/)
(CC BY 4.0) y la geocodificación de respaldo de Nominatim / OpenStreetMap. Este
repositorio **no redistribuye** ninguno de esos feeds: los descarga en tiempo de
ingesta.

## Sobre el uso de IA generativa

Parte de este código se escribió con la asistencia de un modelo de lenguaje
(Claude). La arquitectura y las decisiones de diseño son mías y sé explicarlas, y
varias de las difíciles salieron de depurar contra datos reales: detectar los
cortes de media distancia mediante tramos puenteados por bus, cuando descubrimos
que el feed de larga distancia no publica ni un solo sustitutorio, y el podado
por *slack* que hace que una búsqueda multi-día siga siendo interactiva. Todo lo
que hay aquí lo he revisado, ejecutado y depurado yo contra los GTFS de verdad.
