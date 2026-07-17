# trouble_clock — monitor de reloj de las Control Screens (CS)

Registra en un CSV la diferencia (en **milisegundos**) entre la hora de
**sistema** y del **RTC** de cada Control Screen y la hora del **servidor NTP**,
para graficar la deriva de cada pantalla.

Problema que investigamos:
- La hora de sistema de las CS **se adelanta** respecto al servidor NTP.
- La hora de sistema **no coincide** con el RTC de la CS.

## Arquitectura (recomendada)

```
   Servidor Ubuntu (= servidor NTP)
   └── trouble_clock.py  ──ssh -i system_key──► CS-01 ... CS-05
        │  consulta NTP (SNTP, stdlib)
        └── escribe trouble_clock.csv  ──►  --plot ──► PNG
```

- **[trouble_clock.py](trouble_clock.py)** — corre en el **servidor Ubuntu**, se
  conecta a las Control Screens por **SSH con clave** (`ssh -i system_key root@…`),
  lee su hora de sistema y RTC, consulta el NTP con un cliente **SNTP en stdlib**
  (sin `pip install`) y registra todo en un CSV. Puede graficar con `--plot`.
- **[trouble_clock.sh](trouble_clock.sh)** — versión de respaldo para correr
  **dentro de una sola CS** sin dependencias.

Todas las horas se comparan por **epoch UTC**, así la zona horaria
(`America/Lima -05`) no afecta la comparación.

---

## Versión Python (servidor) — principal

### Requisitos

En el **servidor Ubuntu**:
```bash
python3 --version                  # ya viene en Ubuntu; el script usa solo stdlib
ssh -V                             # openssh-client (ya viene)
chmod 600 system_key               # la clave privada, junto al script

# si la clave tiene passphrase (mssadminkey2018), una de estas dos:
sudo apt install sshpass           # opcion A: el script responde la passphrase
# opcion B (mas limpia): cargarla una vez en el agente
eval $(ssh-agent); ssh-add system_key

# opcional, solo para --plot:
pip3 install matplotlib
```

Prueba manual del acceso a una CS:
```bash
ssh -i system_key root@192.168.3.87 "date -u +'%s %N'"
```

### Configurar las pantallas

Edita la lista al inicio de `trouble_clock.py` con las IP reales:
```python
CONTROL_SCREENS = [
    {"name": "CS-87", "host": "192.168.3.87"},   # 'port' opcional (22 por defecto)
    ...
]
NTP_SERVER = "127.0.0.1"   # el script corre EN el servidor NTP

SSH_USER = "root"
SSH_KEY  = "system_key"    # ruta a la clave privada
```

Todo lo anterior también se puede pasar por variables de entorno sin editar el
archivo: `CS_SSH_USER`, `CS_SSH_KEY`, `CS_SSH_PORT`, `CS_SSH_KEY_PASS`, `NTP_SERVER`,
`CSV_FILE`, `INTERVAL`, `CS_SSH_MUX`, `CS_SSH_PERSIST`.

**Multiplexado ssh (ControlMaster):** activado por defecto en Linux. Mantiene la
conexión ssh viva por cada CS (socket en `$TMPDIR/tc_ssh_mux/`), así solo la primera
lectura paga el handshake (~270 ms) y las siguientes bajan a ~2-5 ms — menos ruido en
los datos. `ControlPersist` (por defecto `10m`) es cuánto sigue viva la conexión entre
mediciones. Desactívalo con `CS_SSH_MUX=0`. En Windows se ignora (openssh no lo soporta).

### Uso

```bash
python3 trouble_clock.py --check                 # prueba NTP y cada CS
python3 trouble_clock.py                         # una medición -> CSV
python3 trouble_clock.py --loop --interval 60    # continuo cada 60 s
python3 trouble_clock.py --plot                  # grafica el CSV a PNG
```

### Dejarlo corriendo siempre

Rápido (sobrevive a cerrar la sesión, no al reboot):
```bash
nohup python3 trouble_clock.py --loop --interval 60 >tc.log 2>&1 &
```

Como servicio systemd (arranca solo tras reboot) — crea
`/etc/systemd/system/trouble-clock.service`:
```ini
[Unit]
Description=Monitor de reloj de Control Screens
After=network-online.target chronyd.service

[Service]
Type=simple
WorkingDirectory=/opt/trouble_clock
Environment=CS_SSH_KEY=/opt/trouble_clock/system_key
Environment=CS_SSH_KEY_PASS=mssadminkey2018
ExecStart=/usr/bin/python3 /opt/trouble_clock/trouble_clock.py --loop --interval 60
Restart=always
User=adminserver

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now trouble-clock
journalctl -u trouble-clock -f
```

### Columnas del CSV

```
fecha_local, iso_utc, cs, host, sys_ms, rtc_ms, ntp_ms,
diff_ntp_sys_ms, diff_ntp_rtc_ms, diff_sys_rtc_ms, ntp_offset_ms, rtt_ms, estado
```

- `diff_ntp_sys_ms` = NTP − Sistema de la CS
  → **negativo = la CS está adelantada** (tu caso); positivo = atrasada.
- `diff_ntp_rtc_ms` = NTP − RTC de la CS.
- `diff_sys_rtc_ms` = Sistema − RTC de la CS.
- `ntp_offset_ms` = offset del propio servidor vs NTP (debería ser ~0).
- `rtt_ms` = ida y vuelta del comando ssh (calidad de la medición; la hora de la
  CS se ubica en el punto medio del round-trip para reducir el error de red).
- `estado` = `ok`, `ssh_error:...`, `sys_no_parseado`, `rtc_no_disponible`, etc.

Graficar: `python3 trouble_clock.py --plot` dibuja `diff_ntp_sys_ms` de las 5 CS
en `trouble_clock.png`. También puedes filtrar por `cs` en Excel.

---

## Versión sh (dentro de una CS) — respaldo

Sin dependencias; corre en el `mksh` nativo de Android. Útil para depurar una
pantalla puntual cuando no hay adb.

```bash
adb push trouble_clock.sh /data/local/tmp/
adb shell
sh /data/local/tmp/trouble_clock.sh --check      # ver qué herramientas hay
sh /data/local/tmp/trouble_clock.sh --loop --interval 60
```
Detalle de métodos y columnas: ver comentarios en el propio script.

---

## Comandos de diagnóstico (referencia)

```bash
# en la CS: desactivar sync automático para observar la deriva
settings put global auto_time 0
settings put global auto_time_zone 0
setprop persist.sys.timezone America/Lima
settings get global ntp_server           # qué NTP usa Android

logcat -v time | grep AlarmManagerService
dmesg | grep -i rtc

# en el servidor NTP de Lima
sudo systemctl status chronyd
sudo chronyc clients
sudo chronyc tracking
```
