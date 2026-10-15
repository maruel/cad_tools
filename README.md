# CAD tools

Shared CAD recipes, mesh validation, print packaging, and tool installers.
Requires Python 3.12+, Git, and uv.

```sh
make verify
```

Projects pin this repository as a submodule.

Initialize a project's recorded checkout once with
`git submodule update --init`; project preparation installs locked
dependencies. The directory is an example: identify the actual submodule by its
URL, `https://github.com/maruel/cad_tools`.

Run ordinary recipes from the project root:

```sh
uv run --locked python scripts/recipe.py
```

Run Blender recipes with `blender.py run scripts/recipe.py`.
`blender.py install` finds the installed app on macOS or caches the shared
pinned release on Linux x64. Discovery validates the pinned version.

`openscad.py` installs, formats, lints, and exports SCAD; `mesh.py` validates and
combines STL. `bambu_studio.py` installs Studio, inspects/packages projects,
analyzes G-code, and builds authored print jobs. Its `install` command reuses an
existing Studio installation or downloads the latest release. Command `--help`
describes inputs.

Signature assets belong in project inputs. Stock is supplied explicitly through
`cad_tools.stock.read_stock(path)` and passed to `resolve_job(job, stock)` or
`build_job(..., stock=stock)`. CLI `build-job` requires `--stock PATH`. Schemas and
validation live in the corresponding source definitions. Use `step.py`
for STEP visualization assemblies and `cad_tools.bambu_job` for 3MF print jobs. See
[AGENTS.md](AGENTS.md) for recipe, workspace, and publication guidance.

`uv run --locked --project PATH_TO_CAD_TOOLS PATH_TO_CAD_TOOLS/step.py VIEWS.json --output OUTPUT_DIR`
exports named, colored STEP visualization assemblies from closed STL meshes.
Run it from the consuming project's root to keep scratch files in its `build/`.
The manifest maps `.step` filenames to part records with `name`, `path`,
`color` (`#RRGGBB`) and optional `translation` (`[x,y,z]` in millimeters).
Paths are relative to the manifest. STEP geometry retains the mesh's planar
facets as solids without fitting curves. The exporter reopens every file and checks
part names, colors, solid validity, bounds and volume before accepting it.

Select an explicit tooling commit to update a project:

```sh
git -C cad_tools checkout COMMIT
git add cad_tools
make prepare
make verify
```
