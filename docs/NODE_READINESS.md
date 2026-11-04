# Preparación de nodos — 4 noviembre 2026

## Verificación del código comprometido

La base se registró en `60e2728` (`feat: bootstrap role-based Apolo nodes`). Se verificaron copias limpias de ese commit en los tres equipos. No se incluyeron datos, pesos, sesiones ni credenciales en Git.

En Zephyrus se clonó la receta en una carpeta nueva y se ejecutó `init --profile workstation --install` sobre un espacio vacío: instaló RAG, knowledge-data y la taxonomía fijados. En Debstat y Hadron se recibieron paquetes Git de la receta y se clonaron en `~/init_node-verified`, sin sobrescribir los checkouts anteriores. Las tres ejecuciones de `doctor` terminaron con `status=ok`.

## Capacidades y límites observados

| Equipo | Comprobación real | Estado para trabajar |
| --- | --- | --- |
| Debstat (`deb`) | Perfil collector, scraper 73b8e4c limpio, entorno Python 3.12 y CLI disponibles; captura universitaria real probada en la iteración previa | Listo para captura HTTP universitaria acotada y feeds de prueba. Falta el productor laboral real |
| Hadron | Korax f377357 limpio, `/health=ok`, `/v1/models` publica `apolo-extractor`; GTX 1080 8 GiB con modelo cargado | Listo para extracción GPU mediante el lote aislado de Korax |
| Zephyrus | Workstation instalado desde clon limpio, entornos y catálogo válidos; API local de desarrollo con conversación, acciones, historial, entrevista y exploración | Listo para importación, RAG y conversaciones de prueba. La CLI completa sigue pendiente de publicación en apolo-rag |

Debstat tenía aproximadamente 2.7 GiB de RAM disponible y uso elevado de swap durante la medición. Se probó captura HTTP pequeña; no se verificó concurrencia elevada ni un navegador Playwright instalado. Hadron tenía unos 374 GiB libres y el modelo ocupaba aproximadamente 2.7 GiB de VRAM; se reutilizó el runtime/modelo existente, no se probó su aprovisionamiento desde una máquina vacía.

## Simulación continua comprobada entre los tres equipos

Se ejecutó `continuous-20261104-b` sobre la instalación limpia de workstation. El collector recibió los feeds simulados por SSH, Hadron ejecutó las extracciones y Zephyrus verificó/importó los resultados y activó las publicaciones aisladas.

| Ciclo | Resultado observado | Extracciones GPU | Publicación |
| --- | --- | --- | --- |
| Inicial | 3 avisos válidos | 3 | Activada |
| Cambios | 1 alta, 1 modificación, 1 retirada, 1 sin cambios; 3 activos | 3 | Activada |
| Repetición | Feed idéntico | 0 | Conservada |
| Fallo controlado | Feed con esquema inválido | 0 | Puntero anterior idéntico byte por byte |
| Recuperación | 4 avisos válidos | 4 | Activada |

El escenario final usó 10 extracciones GPU. La lectura posterior del puntero verificó los hashes de manifiesto, informe y artefactos; el consumidor RAG real devolvió `job_count=4`, `sample_size=4`, Python=4 y SQL=3. Es una prueba de integridad/transporte/publicación; no certifica la exactitud semántica del modelo.

El primer intento detectó una conversión LF→CRLF al transmitir stdin desde Windows. Quedó aislado y se conservó como evidencia de fallo. La corrección usa bytes UTF-8 y comprueba el hash recibido antes de publicar; una prueba real de ida/vuelta con texto Unicode confirmó identidad exacta de bytes antes de repetir el escenario completo.

Verificación final: 55 pruebas pasadas, sin fallos ni omisiones; revisión independiente de las correcciones sin bloqueos. Los informes completos están fuera de Git, bajo `node_workspace/readiness-20261104/` y `node_workspace/verified-workstation/simulations/continuous-20261104-b/`.

La publicación probada usa `simulation-publication-v1`, manifiestos inmutables y un puntero atómico dentro de una simulación explícitamente sintética. El servicio HTTP del chat no sigue automáticamente ese puntero y no se cambió su publicación activa. Ver [CONTINUOUS_SIMULATION.md](CONTINUOUS_SIMULATION.md).

## Interpretación de los diagnósticos

`doctor` comprueba código, entornos y URLs indicadas. No afirma que haya un productor laboral real, una agenda periódica o una publicación de producción activa. El backend de desarrollo de Zephyrus respondió `/ready`, con `publication_id=fixture`: es un entorno de prueba, aunque sus herramientas también se hayan probado con extracciones GPU del lote sintético.

La revisión del CLI del RAG instalado desde el commit publicado 6377060 confirmó que **no incluye `chat`**. La versión de chat completa probada está en el checkout de desarrollo de apolo-rag. Un clon nuevo de la receta no debe afirmar que instala esa capacidad hasta que exista un commit publicado de ese módulo y se actualice el manifiesto.

Los procesos de inferencia y API actuales se iniciaron manualmente. No se verificó recuperación tras reiniciar los PCs. Las claves de OpenRouter permanecen en Zephyrus.

## Pendientes antes de operación continua real

- [Productor laboral real para collector](https://github.com/apoloinc/init_node/issues/1).
- [Contrato universitario compatible con publicación/RAG](https://github.com/apoloinc/apolo-scraper/issues/6).
- Publicar la CLI y las capacidades nuevas de los repositorios dependientes; actualizar los commits fijados tras sus pruebas.
- Integrar el contrato de publicación de producción y un consumidor que siga la publicación activa: [knowledge-data #7](https://github.com/apoloinc/apolo-knowledge-data/issues/7), [RAG #4](https://github.com/apoloinc/apolo-rag/issues/4).
- Aprovisionamiento persistente y verificación tras reinicio, antes de programar capturas reales desatendidas.

Conclusión operativa: los equipos pueden trabajar como nodos de un piloto coordinado; la operación autónoma de producción sigue condicionada a estos pendientes.
