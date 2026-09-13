# AVSync AA - Resumen y handoff para el proximo mes

Fecha de corte: 2026-09-13
Repositorio: https://github.com/Lowitle/avsync
Upstream: https://github.com/stinkybread/avsync

## 1. Objetivo del proyecto

AVSync es una herramienta CLI para sincronizar una pista doblada o extranjera
con la linea temporal de un video de referencia de mayor calidad.

La direccion actual del proyecto es:

- Producto principal: sincronizacion audio-audio (AVSync AA).
- Metodo futuro: sincronizacion video-video (AVSync VV), como fallback cuando
  no existan pistas de audio originales comparables.
- Marca sugerida: AVSync AA - Audio-to-Audio Dubbing Synchronization Engine.

## 2. Lo que se ha conseguido

### Sincronizacion audio-audio

- Se implemento el anclaje mediante pistas de audio originales comparables.
- Se comparan las pistas originales de source y reference mediante correlacion
  de waveform y envolvente de energia.
- Se construye una receta editorial con offsets, drift, cortes, inserciones,
  eliminaciones y tramos reference-only.
- La receta se aplica a la pista doblada y, cuando corresponde, a subtitulos.
- El resultado conserva el video, audio original, capitulos, fuentes y
  metadatos del archivo de referencia.

### Casos editoriales tratados

- Diferencias de FPS y tempo.
- Cortes editoriales y cambios bruscos de offset.
- Eyecatches o material presente solo en reference.
- Tramos censurados, sin doblaje o ausentes en source.
- Finales con distinta duracion.
- Multiples intervalos reference-only dentro del mismo episodio.
- Relleno con audio de reference o silencio mediante
  `--missing_foreign_fill`.
- Comprobacion de empalmes en pausas naturales.
- Ajuste de ganancia cuando se inserta audio de reference.
- Localizacion independiente de bordes de empalme por pista foreign.
- Conservacion de dinamica original: la normalizacion se usa para analizar,
  no para degradar necesariamente el audio final.

### Validacion real

- Flujo One Piece Netflix/Arait validado.
- Batch completo: 53/53 episodios correctos.
- Anclaje validado: japones reference contra japones source.
- Pista final validada: doblaje espanol Netflix.
- Politica usada en el batch:

```text
--anchor_source audio
--ref_stream_idx 9
--foreign_stream_idx 1
--foreign_anchor_stream_idx 2
--foreign_tracks 1
--foreign_lang spa
--missing_foreign_fill reference
--no_subtitles
```

- E01 y E08 sirvieron para afinar bordes de reemplazo.
- E02 valido la receta unificada de transicion con racha anterior, banda
  ambigua, racha posterior y seleccion de un punto de baja energia.
- La localizacion por pista fue validada con una regresion sintetica
  determinista en `test_per_track_splice_localization.py`.

### Herramientas y modos añadidos

- `--visual_map_only` para analizar el mapa visual sin generar salida final.
- `--visual_map_report_csv` para exportar el mapa visual.
- `--visual_map_qc_dir` para generar material QC opcional.
- `--missing_foreign_fill {auto,reference,silence}`.
- Informes de anclas, transiciones, segmentos y seguridad de empalmes.
- Mejoras de deteccion VFR/AVI usando `avg_frame_rate`.
- Correccion de parsing de `pts_time` de FFmpeg.
- Correccion de generacion de silencio mono/estereo.
- Correcciones de logs, rutas absolutas de concat y procesamiento batch.
- Version de checkpoint/cache actual: 20.

## 3. Estado de publicacion

- El repositorio de trabajo es `Lowitle/avsync`.
- Tiene `origin` apuntando a `https://github.com/Lowitle/avsync.git`.
- Tiene `upstream` apuntando a `https://github.com/stinkybread/avsync.git`.
- El branch publicado es `main`.
- El push de la release publica inicial se completo correctamente.
- El ultimo commit publicado es `27c113f Prepare repo for public GitHub release`.
- El repositorio sigue siendo un fork de GitHub, pero se mantiene como linea
  de trabajo propia. No es obligatorio crear otro repositorio.
- No existe todavia una configuracion de paquete `pyproject.toml` en este repo.
  Packaging/PyPI se dejo para una fase posterior y no bloquea el lanzamiento.
- GitHub Sponsors/Open Collective aun no estan activados.
- No se ha creado todavia una release/tag publico definitivo, aunque la
  recomendacion acordada es `v1.0.0`.

## 4. Estado del working tree al hacer este documento

La rama estaba sincronizada con `origin/main`, pero habia un cambio local
pendiente en `.gitignore` y archivos de utilidad locales ignorados:

- `scan_noise_floor.py`
- `test_independent_replacement_relocation.py`
- `test_with_loudness.py`

Antes de otra release conviene ejecutar:

```powershell
git status --short --branch
git diff -- .gitignore
```

Si el cambio de `.gitignore` es el esperado, hacer commit y push. No borrar
utilidades locales sin revisar su utilidad primero.

## 5. Proximos pasos recomendados

### Prioridad 1: cerrar la publicacion inicial

1. Revisar en GitHub que el repo sea visible publicamente.
2. Usar como nombre visible: `AVSync AA - Audio-to-Audio Dubbing Synchronization Engine`.
3. Usar como descripcion corta:

   `Audio-to-audio synchronization for remastering dubbed tracks onto a reference timeline while preserving the original video and metadata.`

4. Crear el tag y release `v1.0.0`.
5. Usar release notes centradas en AA validado, 53/53 episodios y VV como
   roadmap, sin presentar VV como funcionalidad terminada.
6. Revisar la licencia upstream y la atribucion del fork antes de promocionarlo
   ampliamente.

### Prioridad 2: comunicacion y feedback

1. Publicar el anuncio en Discussions del repo original con tono respetuoso.
2. Publicar el post preparado en VideoHelp.
3. Enlazar siempre el fork:
   `https://github.com/Lowitle/avsync`
4. Pedir pruebas reproducibles y feedback sobre casos con audio original
   comparable, diferencias editoriales y source de baja calidad.
5. Evitar afirmar que el fork es universalmente mejor; describirlo como una
   evolucion especializada y validada para AA.

### Prioridad 3: financiacion

1. Activar GitHub Sponsors en la cuenta u organizacion propia.
2. Crear o conectar Open Collective propio si se desea usarlo como fiscal host.
3. Añadir el enlace de apoyo al README, al perfil y a la release.
4. No depender del autor upstream para crear el collective o repartir fondos.
5. Contactar al autor original solo para informar del fork, agradecer el trabajo
   y abrir una posible colaboracion tecnica.

### Prioridad 4: QA antes de prometer mas

1. Escuchar una muestra representativa del batch One Piece final.
2. Revisar especialmente episodios con `Located missing foreign interval`.
3. Confirmar nombres y metadatos de pistas con MKVToolNix si se van a publicar
   ejemplos.
4. Preparar un pequeno conjunto de casos reproducibles sin redistribuir
   material con copyright.
5. Añadir instrucciones de instalacion local mas claras si llegan usuarios.

### Prioridad 5: AVSync VV

No reabrir la implementacion VV hasta cerrar la publicacion AA. La hoja de ruta
ya definida es:

1. Mejorar el modo visual de analisis CSV/QC.
2. Normalizar tiempos visuales por FPS sin reencodear.
3. Clasificar transiciones: negro, fade, imagen-imagen y ambiguas.
4. Construir un offset visual denso y detectar saltos editoriales.
5. Refinar bordes de transicion con evidencia y confianza.
6. Convertir los hallazgos visuales a la misma receta comun que AA.
7. Validar primero un episodio, luego un batch pequeno y finalmente un batch.

## 6. Mensaje para retomar el trabajo

Copiar y pegar este mensaje al volver:

> Retomamos AVSync AA desde `C:\Users\jmbos\Documents\GitHub\avsync`.
> Lee primero `HANDOFF_PROXIMO_MES.md` y `OBJETIVOS_INMEDIATOS.txt`.
> El AA esta validado con 53/53 episodios One Piece y el repo publico es
> `https://github.com/Lowitle/avsync`, con upstream en
> `https://github.com/stinkybread/avsync`. Primero verifica `git status`,
> `.gitignore`, tags/releases y la visibilidad del repo. Despues continua por
> este orden: cerrar `v1.0.0`, revisar README y atribucion/licencia, preparar
> Sponsors/Open Collective propios, y solo despues retomar AVSync VV. No
> reescribas el AA validado ni hagas cambios amplios sin una prueba focalizada.

## 7. Decisiones que no deben perderse

- AA es el producto actual; VV es roadmap.
- El fork puede mantenerse como proyecto propio sin crear otro repo.
- La financiacion debe estar vinculada a la cuenta u organizacion propia.
- Packaging/PyPI no bloquea la publicacion inicial.
- La evidencia mas fuerte es el batch real 53/53 y las revisiones auditivas,
  no una afirmacion general de superioridad.
- Mantener credito y respeto hacia `stinkybread/avsync`.
- No incluir videos, audios o ejemplos redistribuibles con copyright en el repo.
