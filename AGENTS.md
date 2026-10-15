# Working with CAD tools

Read README.md for setup and entrypoints. Keep schemas and implementation
contracts with their owning Python definitions.

- Use locked uv environments. Run `make verify` for tooling changes.
- Scripts resolve required import paths from `__file__` before importing shared
  tools or project helpers. Blender scripts parse their arguments after `--`.
- Call `prepare_workspace()` before recipe work. Keep temporary data, logs,
  bytecode, and diagnostic sheets under the project's ignored `build/` directory.
  Run commands from the project root.
- Publish complete validated staging trees on the destination filesystem through
  `cad_tools.publication.publish`. Staging is consumed; regenerate it after an
  interruption or another publication.
- Shared installers and discovery own tool versions and platform handling;
  project recipes invoke those boundaries.
- Keep signature assets in project inputs. Stock is an explicit generic input;
  personal inventory sources and refresh logic belong outside this repository.
- On CAD backend changes, compare mesh closure, dimensions, volume, and repeated
  STL bytes. Source-format diagnostics do not replace geometry validation.
