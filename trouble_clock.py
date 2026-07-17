#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trouble_clock.py
Monitor de reloj de Control Screens (CS) - Marcobre.

Corre en el SERVIDOR Ubuntu (que ademas es el servidor NTP) y se conecta por
SSH a varias Control Screens para leer su hora de sistema y su RTC. Compara
todo contra la hora NTP y registra en un CSV las diferencias en milisegundos,
para graficar la deriva de cada pantalla.

Cliente NTP (SNTP) en stdlib puro; la lectura de las CS es por ssh:
    ssh -i system_key root@<host>

Requisitos en el servidor:
    - 'ssh' (openssh-client, ya viene en Ubuntu)
    - la clave privada 'system_key' accesible (chmod 600 system_key)
    - si la clave tiene passphrase: 'sshpass' (sudo apt install sshpass)
      o cargarla una vez con: eval $(ssh-agent); ssh-add system_key

Uso:
    python3 trouble_clock.py --check                 # prueba NTP y cada CS
    python3 trouble_clock.py                         # una medicion -> CSV
    python3 trouble_clock.py --loop --interval 60    # continuo cada 60 s
    python3 trouble_clock.py --plot                  # grafica el CSV (matplotlib)

Dejarlo corriendo siempre:
    nohup python3 trouble_clock.py --loop --interval 60 >tc.log 2>&1 &
    # o crear un servicio systemd (ver README).
"""

import argparse
import csv
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

# =============================== CONFIGURACION ===============================

# Servidor NTP a consultar. Como el script corre EN el servidor NTP,
# "127.0.0.1" apunta a chrony local. Cambia a la IP si consultas otro.
NTP_SERVER = os.environ.get("NTP_SERVER", "127.0.0.1")

# Acceso SSH a las Control Screens:  ssh -i system_key root@<host>
SSH_USER = os.environ.get("CS_SSH_USER", "root")
SSH_KEY = os.environ.get("CS_SSH_KEY", "system_key")   # ruta a la clave privada
SSH_PORT = int(os.environ.get("CS_SSH_PORT", "22"))
# Passphrase de la clave. SEGURIDAD: mejor exportar CS_SSH_KEY_PASS o usar
# ssh-agent (ssh-add system_key) y dejar esto vacio.
SSH_KEY_PASS = os.environ.get("CS_SSH_KEY_PASS", "mssadminkey2018")

# Multiplexado ssh (ControlMaster): mantiene viva la conexion por CS para que
# las lecturas siguientes no paguen el handshake (rtt ~270ms -> ~2-5ms).
# Solo POSIX (openssh de Windows no soporta multiplexado).
SSH_MUX = os.environ.get("CS_SSH_MUX", "1") not in ("0", "", "false", "no")
SSH_PERSIST = os.environ.get("CS_SSH_PERSIST", "10m")   # cuanto queda viva
_MUX_DIR = os.path.join(tempfile.gettempdir(), "tc_ssh_mux")

# Las Control Screens a monitorear. Rellena las IP; 'port' opcional (22).
CONTROL_SCREENS = [
    {"name": "CS-87", "host": "192.168.3.87"},
    # {"name": "CS-02", "host": "192.168.3.xx"},
    # {"name": "CS-03", "host": "192.168.3.xx"},
    # {"name": "CS-04", "host": "192.168.3.xx"},
    # {"name": "CS-05", "host": "192.168.3.xx"},
]

CSV_FILE = os.environ.get("CSV_FILE", "trouble_clock.csv")
INTERVAL = int(os.environ.get("INTERVAL", "60"))     # segundos entre mediciones
SSH_TIMEOUT = 12                                     # seg por comando ssh
NTP_TIMEOUT = 5                                      # seg para la consulta NTP

CSV_COLUMNS = [
    "fecha_local", "iso_utc", "cs", "host",
    "sys_ms", "rtc_ms", "ntp_ms",
    "diff_ntp_sys_ms", "diff_ntp_rtc_ms", "diff_sys_rtc_ms",
    "ntp_offset_ms", "rtt_ms", "estado",
]

# ============================= CLIENTE NTP (SNTP) ============================

_NTP_DELTA = 2208988800  # segundos entre 1900-01-01 y 1970-01-01


def ntp_query(host, port=123, timeout=NTP_TIMEOUT):
    """Consulta SNTP. Devuelve dict con offset y delay (seg) y hora NTP.

    offset = (NTP - reloj_local_del_servidor)  [algoritmo estandar NTP].
    Lanza excepcion si el servidor no responde.
    """
    pkt = b"\x1b" + 47 * b"\0"          # LI=0, VN=3, Mode=3 (client)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        t0 = time.time()
        s.sendto(pkt, (host, port))
        data, _ = s.recvfrom(1024)
        t3 = time.time()
    finally:
        s.close()
    if len(data) < 48:
        raise ValueError("respuesta NTP corta")
    u = struct.unpack("!12I", data[:48])
    t1 = u[8] + u[9] / 2**32 - _NTP_DELTA    # server receive
    t2 = u[10] + u[11] / 2**32 - _NTP_DELTA  # server transmit
    offset = ((t1 - t0) + (t2 - t3)) / 2.0
    delay = (t3 - t0) - (t2 - t1)
    return {"offset": offset, "delay": delay, "ntp_epoch": t2}


# =============================== LECTURA CS (ssh) ============================

# Comando remoto: linea1 = "seg nano" de sistema (UTC); linea2 = RTC.
_REMOTE_CMD = (
    "date -u +'%s %N'; "
    "cat /sys/class/rtc/rtc0/since_epoch 2>/dev/null || "
    "cat /sys/class/rtc/rtc1/since_epoch 2>/dev/null || "
    "hwclock 2>/dev/null || hwclock --show 2>/dev/null"
)


def ssh_argv(host, port, remote_cmd):
    """Construye el argv para leer la CS por ssh -i <clave> root@host.

    Si hay passphrase configurada y 'sshpass', la responde automaticamente.
    Si no, confia en ssh-agent o en una clave sin passphrase (BatchMode).
    """
    opts = [
        "-i", SSH_KEY,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"ConnectTimeout={SSH_TIMEOUT}",
        "-p", str(port),
    ]
    if SSH_MUX and os.name == "posix":
        try:
            os.makedirs(_MUX_DIR, mode=0o700, exist_ok=True)
        except OSError:
            pass
        opts += [
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={_MUX_DIR}/%C",   # %C = hash corto (evita socket largo)
            "-o", f"ControlPersist={SSH_PERSIST}",
        ]
    dest = f"{SSH_USER}@{host}"
    if SSH_KEY_PASS and shutil.which("sshpass"):
        # -P assphrase: sshpass responde al prompt "Enter passphrase for key..."
        return ["sshpass", "-P", "assphrase", "-p", SSH_KEY_PASS,
                "ssh", *opts, dest, remote_cmd]
    # sin sshpass: requiere ssh-agent o clave sin passphrase
    return ["ssh", "-o", "BatchMode=yes", *opts, dest, remote_cmd]


_hint_shown = False


def _auth_hint():
    """Sugerencia (una sola vez) cuando ssh no puede usar la clave."""
    global _hint_shown
    if _hint_shown:
        return
    _hint_shown = True
    if SSH_KEY_PASS and not shutil.which("sshpass"):
        print(
            "[ayuda] La clave tiene passphrase y no hay 'sshpass'. Carga la clave\n"
            "        en el agente para que ssh autentique solo:\n"
            "          Linux:   eval $(ssh-agent); ssh-add " + SSH_KEY + "\n"
            "          Windows: Start-Service ssh-agent; ssh-add " + SSH_KEY + "\n"
            "        o instala sshpass (Ubuntu: sudo apt install sshpass).",
            file=sys.stderr)


def ssh_run(host, port, remote_cmd):
    """Corre el comando remoto por ssh. Devuelve (rc, stdout, stderr)."""
    try:
        p = subprocess.run(
            ssh_argv(host, port, remote_cmd),
            capture_output=True, text=True, timeout=SSH_TIMEOUT + 3,
        )
        if p.returncode != 0 and "Permission denied" in p.stderr:
            _auth_hint()
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError as e:
        return 127, "", str(e)


def _parse_sys_line(line):
    """'1752489458 123456789' -> epoch en ms (float). Tolera sin %N."""
    parts = line.split()
    if not parts or not parts[0].isdigit():
        return None
    s = int(parts[0])
    ns = 0
    if len(parts) > 1 and parts[1].isdigit():
        ns = int(parts[1])
    return s * 1000.0 + ns / 1_000_000.0


def _parse_rtc_line(line):
    """Devuelve epoch en ms (float) del RTC. Acepta since_epoch o hwclock."""
    line = line.strip()
    if not line:
        return None
    if line.isdigit():                       # /sys/.../since_epoch (segundos)
        return int(line) * 1000.0
    # hwclock, ej: "2026-07-14 14:17:38+0000" o "2026-07-14 14:17:38.123+00:00"
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(\.\d+)?", line)
    if not m:
        return None
    frac = float(m.group(3)) if m.group(3) else 0.0
    dt = datetime.strptime(m.group(1) + " " + m.group(2), "%Y-%m-%d %H:%M:%S")
    dt = dt.replace(tzinfo=timezone.utc)     # hwclock se muestra en UTC
    return dt.timestamp() * 1000.0 + frac * 1000.0


def read_cs(cs):
    """Lee una Control Screen. Devuelve dict con sys_ms, rtc_ms, rtt_ms, estado.

    sys/rtc se leen en una sola llamada ssh; sys corresponde ~ al punto medio
    (t0+t1)/2 del round-trip, cuyo valor tambien se devuelve como 'mid_epoch_ms'.
    """
    host = cs["host"]
    port = cs.get("port", SSH_PORT)
    res = {"sys_ms": None, "rtc_ms": None, "rtt_ms": None,
           "mid_epoch_ms": None, "estado": "ok"}

    t0 = time.time()
    rc, out, err = ssh_run(host, port, _REMOTE_CMD)
    t1 = time.time()
    if rc != 0 or not out:
        res["estado"] = f"ssh_error:{(err or 'sin salida')[:60]}"
        return res

    res["rtt_ms"] = round((t1 - t0) * 1000.0, 1)
    res["mid_epoch_ms"] = (t0 + t1) / 2.0 * 1000.0

    lines = out.splitlines()
    res["sys_ms"] = _parse_sys_line(lines[0]) if lines else None
    res["rtc_ms"] = _parse_rtc_line(lines[1]) if len(lines) > 1 else None
    if res["sys_ms"] is None:
        res["estado"] = "sys_no_parseado"
    elif res["rtc_ms"] is None:
        res["estado"] = "rtc_no_disponible"
    return res


# =============================== MEDICION ===================================

def measure_all(ntp):
    """Mide las 5 CS contra el NTP dado. Devuelve lista de filas (dict CSV).

    ntp: dict de ntp_query() o None si el NTP no respondio.
    """
    now = datetime.now()
    fecha_local = now.strftime("%Y-%m-%d %H:%M:%S")
    iso_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    offset_ms = ntp["offset"] * 1000.0 if ntp else None

    rows = []
    for cs in CONTROL_SCREENS:
        r = read_cs(cs)
        sys_ms = r["sys_ms"]
        rtc_ms = r["rtc_ms"]

        # Hora NTP en el instante en que se leyo la CS (punto medio del rtt):
        ntp_ms = None
        if ntp and r["mid_epoch_ms"] is not None:
            ntp_ms = r["mid_epoch_ms"] + offset_ms

        def dif(a, b):
            return round(a - b, 1) if (a is not None and b is not None) else None

        rows.append({
            "fecha_local": fecha_local,
            "iso_utc": iso_utc,
            "cs": cs["name"],
            "host": cs["host"],
            "sys_ms": round(sys_ms, 1) if sys_ms is not None else None,
            "rtc_ms": round(rtc_ms, 1) if rtc_ms is not None else None,
            "ntp_ms": round(ntp_ms, 1) if ntp_ms is not None else None,
            "diff_ntp_sys_ms": dif(ntp_ms, sys_ms),
            "diff_ntp_rtc_ms": dif(ntp_ms, rtc_ms),
            "diff_sys_rtc_ms": dif(sys_ms, rtc_ms),
            "ntp_offset_ms": round(offset_ms, 3) if offset_ms is not None else None,
            "rtt_ms": r["rtt_ms"],
            "estado": r["estado"],
        })
    return rows


def append_csv(rows):
    nuevo = not os.path.exists(CSV_FILE) or os.path.getsize(CSV_FILE) == 0
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if nuevo:
            w.writeheader()
        for row in rows:
            w.writerow(row)


def get_ntp():
    """Consulta el NTP; imprime aviso y devuelve None si falla."""
    try:
        return ntp_query(NTP_SERVER)
    except Exception as e:  # noqa: BLE001
        print(f"[aviso] NTP {NTP_SERVER} no respondio ({e}); "
              f"las columnas NTP quedaran vacias.", file=sys.stderr)
        return None


def run_once():
    ntp = get_ntp()
    rows = measure_all(ntp)
    append_csv(rows)
    _print_rows(rows)


def run_loop(interval):
    print(f"Registrando en {CSV_FILE} cada {interval}s. Ctrl-C para detener.",
          file=sys.stderr)
    try:
        while True:
            run_once()
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nDetenido.", file=sys.stderr)


def _print_rows(rows):
    for r in rows:
        print(f"{r['fecha_local']}  {r['cs']:6} "
              f"NTP-sys={_fmt(r['diff_ntp_sys_ms'])}ms  "
              f"NTP-rtc={_fmt(r['diff_ntp_rtc_ms'])}ms  "
              f"sys-rtc={_fmt(r['diff_sys_rtc_ms'])}ms  "
              f"rtt={_fmt(r['rtt_ms'])}ms  [{r['estado']}]")


def _fmt(v):
    return "  ----" if v is None else f"{v:+.0f}".rjust(6)


# =============================== --check ====================================

def do_check():
    print("== trouble_clock.py :: chequeo ==")
    print(f"ssh              : {shutil.which('ssh') or 'NO ENCONTRADO'}")
    sp = shutil.which("sshpass")
    print(f"sshpass          : {sp or 'no (usa ssh-agent o clave sin passphrase)'}")
    print(f"clave ssh        : {SSH_KEY} "
          f"({'existe' if os.path.exists(SSH_KEY) else 'NO EXISTE'})")
    print(f"acceso           : ssh -i {SSH_KEY} {SSH_USER}@<host>:{SSH_PORT}")
    mux = "activo" if (SSH_MUX and os.name == "posix") else "no (Windows o desactivado)"
    print(f"multiplexado ssh : {mux} (persist={SSH_PERSIST})")
    ntp = None
    try:
        ntp = ntp_query(NTP_SERVER)
        print(f"NTP {NTP_SERVER:15}: OK  offset={ntp['offset']*1000:+.2f} ms  "
              f"delay={ntp['delay']*1000:.2f} ms")
    except Exception as e:  # noqa: BLE001
        print(f"NTP {NTP_SERVER:15}: FALLO ({e})")
    print(f"CSV destino      : {CSV_FILE}\n")
    print("-- Control Screens --")
    for cs in CONTROL_SCREENS:
        r = read_cs(cs)
        print(f"  {cs['name']:6} {cs['host']:15} -> {r['estado']}"
              + (f"  rtt={r['rtt_ms']}ms" if r['rtt_ms'] is not None else ""))
    print("\n-- medicion de prueba --")
    _print_rows(measure_all(ntp))


# =============================== --plot =====================================

def do_plot(png="trouble_clock.png"):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except ImportError:
        print("Falta matplotlib: pip3 install matplotlib", file=sys.stderr)
        sys.exit(1)
    if not os.path.exists(CSV_FILE):
        print(f"No existe {CSV_FILE}", file=sys.stderr)
        sys.exit(1)

    series = {}  # cs -> (times[], diff_ntp_sys[])
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            v = row["diff_ntp_sys_ms"]
            if not v:
                continue
            try:
                t = datetime.strptime(row["iso_utc"], "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue
            series.setdefault(row["cs"], ([], []))
            series[row["cs"]][0].append(t)
            series[row["cs"]][1].append(float(v))

    fig, ax = plt.subplots(figsize=(11, 5))
    for cs, (ts, ys) in sorted(series.items()):
        ax.plot(ts, ys, marker=".", label=cs)
    ax.axhline(0, color="gray", lw=0.8)
    ax.set_title("Deriva de reloj: NTP - Sistema por Control Screen")
    ax.set_xlabel("UTC")
    ax.set_ylabel("NTP - Sistema (ms)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate()
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(png, dpi=120)
    print(f"Grafico guardado en {png}")


# =============================== MAIN =======================================

def main():
    ap = argparse.ArgumentParser(description="Monitor de reloj de Control Screens (Marcobre).")
    ap.add_argument("--check", action="store_true", help="prueba NTP y cada CS")
    ap.add_argument("--loop", action="store_true", help="mide en bucle")
    ap.add_argument("--interval", type=int, default=INTERVAL, help="seg entre mediciones (loop)")
    ap.add_argument("--csv", help="ruta del CSV de salida")
    ap.add_argument("--plot", action="store_true", help="grafica el CSV a PNG")
    args = ap.parse_args()

    global CSV_FILE
    if args.csv:
        CSV_FILE = args.csv

    if args.plot:
        do_plot()
    elif args.check:
        do_check()
    elif args.loop:
        run_loop(args.interval)
    else:
        run_once()


if __name__ == "__main__":
    main()
