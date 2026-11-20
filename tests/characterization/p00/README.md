# P00 · Compatibilidad de LangGraph en Forge 0.2.0

Ejecutar en una VM Linux de Vast con `FORGE_VAST_LAB_ID` igual al ID verificado en el inventario. El ensayo usa una lectura HTTP en loopback, dos tickets sintéticos y un proceso nuevo por ruta. `P00-S01` debe producir un `GET` al sistema de tickets. `P00-S02` es el control que no debe producir llamadas. El destino conserva sus observaciones en el proceso que dirige el ensayo.

## Perfiles de instalación

Usar entornos virtuales nuevos y guardar el comando de instalación, sus tiempos, los artefactos y las versiones resueltas junto con los resultados. Ejecutar una vez por versión de Python que se quiera caracterizar.

| Perfil | Preparación |
| --- | --- |
| `public-wheel` | Instalar desde el índice público `forge-os[langgraph,review]==0.2.0` y sus dependencias. Usar el backend CPU para PyTorch si se instala con `uv`. Registrar el origen y los SHA-256 de las ruedas de Forge. |
| `checkout` | Materializar `7b8ea1e4a081cb41ead499029492e2ee5b286be3` en un checkout limpio. Instalar como editables `forge-os`, `forge-os-core`, `forge-os-cli`, `forge-os-observe`, `forge-os-memory`, `forge-os-adapters` y `forge-review` a partir de ese árbol. Para comparar sólo el origen de Forge, conservar las versiones transitivas resueltas del perfil público. |

Desde la VM, con el arnés en `$HARNESS`, ejecutar cada perfil con su intérprete y un directorio nuevo fuera del checkout.

```bash
"$PUBLIC_ENV/bin/python" "$HARNESS/run.py" \
  --profile public-wheel --output "$EVIDENCE/public-wheel"
"$CHECKOUT_ENV/bin/python" "$HARNESS/run.py" \
  --profile checkout --checkout "$AUDITED_CHECKOUT" --output "$EVIDENCE/checkout"
```

`p00.json` registra el resultado del ensayo, los comandos, los códigos y lo que recibió el destino. `environment.json` conserva la procedencia de las importaciones de Forge y el grafo de dependencias instalado. Cada ruta tiene sus propios stdout y stderr, y el SDK también guarda un resultado JSON. La CLI `wrap` de esta versión expone su resultado como texto. Las dos variantes del orden de argumentos desactivan memoria y habilitan el detalle de la salida.

## Discrepancias y pruebas de cierre

| Discrepancia | Evidencia del ensayo | Responsable y cierre |
| --- | --- | --- |
| El comando documentado puede rechazar `SOURCE` si las opciones siguen al archivo. El orden inverso permite distinguir ese caso de un fallo de descubrimiento de adaptadores. | El código de proceso, el diagnóstico específico y cero lecturas comprobadas por el destino. El grafo nativo y el adaptador explícito deben completar ambos casos de control. | Ingeniería de Forge. Una corrección de compatibilidad de `wrap` repite el comando documentado sobre el wheel y requiere la misma lectura que el SDK. |
| La topología reportada puede perder un nodo que el grafo compilado todavía ejecuta. | `mutation.json` registra la topología antes y después del cull. El destino recibe la lectura del nodo que desapareció de la topología. | Ingeniería de Forge y responsable del binding, F2/P05. Comprobar una configuración efectiva nueva en el destino independiente. |
| La decisión de `ReviewRunner.pre_apply` y la aplicación de cambios por `EvolutionLoop.step` son rutas separadas. | El control de revisión rechaza el cull del nodo obligatorio. La ruta automática realiza el cull y cambia la topología del adaptador sin llamar al hook. | Ingeniería de Forge, F3/P10. Demostrar ausencia de activación en el destino al rechazar una propuesta manual o automática. |

Estas comprobaciones caracterizan las rutas sintéticas seleccionadas. La selección del piloto, la clasificación de sus rutas y la aprobación del resultado esperado corresponden al dueño del agente y al dueño del flujo de negocio.
