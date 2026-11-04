# init_node

[Preparación verificada de nodos](docs/NODE_READINESS.md) · [Simulación de publicación continua](docs/CONTINUOUS_SIMULATION.md).

[Distribución actual, pruebas entre equipos y pendientes](docs/ITERACION_20261104.md).

Una receta común para instalar nodos APOLO por responsabilidad, en Windows o Linux.

| Perfil | Módulos | Equipo probado |
| --- | --- | --- |
| `coordinator` | Datos, Korax, taxonomía | Zephyrus |
| `rag` | RAG, datos/resolver, taxonomía | Debstat (`deb`) |
| `inference` | Korax | Hadron |
| `collector` | Scraper de trabajos y universidades | Debstat (`deb`) |
| `workstation` | RAG, datos/resolver y taxonomía | Zephyrus |

## Instalación

Requisitos: Git, Python >=3.10 y acceso de lectura a los repositorios privados. Datos/RAG requieren uv y Python 3.12 (`uv python install 3.12`). El perfil inference usa la biblioteca estándar; su modelo y runtime se aprovisionan aparte.

```sh
git clone https://github.com/apoloinc/init_node.git
cd init_node
python node.py init --profile coordinator --root node_workspace --install
python node.py doctor --root node_workspace
```

En Linux usar `python3`. En Debstat: `python3 node.py init --profile collector --root ~/apolo-node --install`. En Zephyrus: `python node.py init --profile workstation --root node_workspace --install`. En Hadron se conserva `inference`. Zephyrus recibe los datos y ejecuta RAG/conversaciones; configurar `OPENROUTER_API_KEY` localmente al lanzar RAG, sin guardarla en el repositorio.

`manifest.json` fija commits publicados. La instalación crea repositorios aislados, entornos Python y el catálogo generado. Repetir `init` verifica versiones y rechaza cambios locales, orígenes diferentes o revisiones inesperadas. Para cambiar de versiones, usar un manifiesto y una raíz nuevos.

El módulo de datos usa `locks/knowledge.txt`; RAG conserva su `uv.lock` publicado. No se incorporan los cambios pendientes de los repositorios vecinos.

## Comunicación y salud

```sh
python node.py check-peers --topology examples/topology.json
python node.py doctor --root node_workspace --url http://127.0.0.1:8080/health
```

Los alias SSH del ejemplo pertenecen al coordinador. `check-peers` verifica ida/vuelta de un token. `doctor` verifica estado, módulos, revisiones, entornos Python, catálogo y las URLs solicitadas; falla con código distinto de cero. Sin `--url`, no afirma que los servicios estén corriendo.

Para nodos sin credenciales privadas: `init --bundle-dir /ruta/paquetes`, con archivos `<módulo>.bundle` exportados desde los commits fijados.

[Receta de operación y piloto entre tres equipos](docs/OPERACION.md): comandos de transferencia, inferencia, importación y comprobación con el resolver real. Incluye requisitos del runtime/modelo y límites conocidos de la versión publicada de RAG.

## Verificación

```sh
python -m unittest discover -s tests -q
```

Se probaron instalaciones en Windows y Linux, reinicialización, paquetes Git y un piloto GPU de 25 avisos sintéticos. El bootstrap no instala servicios de arranque ni modifica los workers históricos.
