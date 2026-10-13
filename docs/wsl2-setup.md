# Simulation setup

Run PyBullet under Linux or WSL2. The setup helper creates the simulation environment:

```sh
bash tools/wsl2-setup.sh
```

Alternatively install `pybullet==3.2.7` in a Python environment with a compatible wheel. From the repository root:

```sh
python sim/acceptance_t13.py --help
python sim/acceptance_hc.py --help
```

Use a Linux directory for compiled dependencies. WSLg is required for an interactive GUI; headless tests do not require it.
