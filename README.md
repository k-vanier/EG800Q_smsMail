# EG800Q_smsMail

SMS to e-mail on FreeBSD. An SMS sent to a **Quectel EG800Q-EU** 4G module from an allowed number is sent on by e-mail. The module sends the mail itself, with its own SMTP client (`AT+QSMTP...`) over its mobile data connection: the FreeBSD machine only drives it over USB and needs no Internet access. It runs as a FreeBSD service, starts at boot, and logs every SMS.

> **Status:** the AT command flow is checked against a simulated module only. It has not yet sent a mail through the real module.

## How it works

```
phone ──SMS "toto@exemple.fr Je rentre à 19h"──▶ EG800Q-EU ──USB──▶ FreeBSD: sms_mail.py
                                                 EG800Q-EU ◀──USB── AT+QSMTP... ◀──┘
                     toto@exemple.fr ◀──mail── EG800Q-EU (SMTP over 4G)
phone ◀──SMS "Mail envoyé à toto a exemple.fr"── EG800Q-EU
```

1. `sms_mail.py` drives the module with AT commands over its USB port, and lists the SMS stored in the module every 5 seconds, and 1 second after the module announces a new one (`+CMTI`).
2. SMS from one number less than 5 seconds apart are joined, so a long message split by the phone is mailed once.
3. An SMS from a number outside `--allow` is ignored. Otherwise the address at the start of the SMS is the recipient and the rest is the body; with `--to`, an SMS without an address goes to that address.
4. The script opens the data connection if needed (`AT+QIACT=1`), sets the SMTP account, then sends the mail (`AT+QSMTPDST`, `QSMTPSUB`, `QSMTPBODY`, `QSMTPPUT`) and waits for its result (`+QSMTPPUT: <err>,<SMTP code>`).
5. It answers the sender by SMS, then deletes the SMS from the module, whose memory only holds 10 messages.

## Hardware

| | |
|---|---|
| Module | Quectel EG800Q-EU (USB ID `2c7c:6007`), on a SIM with mobile data |
| Firmware | `EG800QEULCR01A11M04` or later: SMTP is listed from A11 in Quectel's release notes |

**Wiring: use the module's own USB-C port.** FreeBSD has no driver for its serial interfaces, so the script talks to its AT interface directly with pyusb (port `usb`). The module's UART through a USB-serial adapter also works (port `/dev/cuaU0`, or `auto` for an FTDI adapter).

Only one program may read the module's SMS at a time: never run `sms_mail.py` next to `sms_claude.py` ([EG800Q_smsResponder](https://github.com/k-vanier/EG800Q_smsResponder)). `rc.d/sms_mail` refuses to start while the `sms_claude` service runs.

## Files

| File | Purpose |
|---|---|
| `tools/sms_mail.py` | The SMS to e-mail service |
| `tools/eg800q.py` | Shared code: AT commands over USB or UART, GSM text encoding, SMS loop, SMS log |
| `rc.d/sms_mail` | FreeBSD service: runs `sms_mail.py` at boot under `daemon(8)` |
| `etc/sms_mail.conf.sample` | SMTP account, to copy to `/usr/local/etc/sms_mail.conf` |
| `devd/eg800q.conf` | devd rule: gives the module's USB device to the `dialer` group when it is plugged in |
| `newsyslog/sms.conf` | Monthly rotation of `/var/log/sms` |

## Installation on FreeBSD

`youruser` is the unprivileged account that will run the service. Run the commands as root, from this folder.

1. Packages (the `py312-` prefix follows the default Python version: `pkg search pyserial`):

   ```
   pkg install python3 py312-pyserial py312-pyusb
   ```

2. The user must be in the `dialer` group, which gets access to the module (log out and back in afterwards):

   ```
   pw groupmod dialer -m youruser
   ```

3. USB access: the devd rule gives the module, and only it, to `dialer` each time it is plugged in. Unplug and plug it back, then `ls -lL /dev/ugen*` must show group `dialer`, mode `crw-rw----`.

   ```
   mkdir -p /usr/local/etc/devd
   install -m 444 devd/eg800q.conf /usr/local/etc/devd/
   service devd restart
   ```

4. The SMTP account, readable by `youruser` only since it holds the password:

   ```
   install -o youruser -m 600 etc/sms_mail.conf.sample /usr/local/etc/sms_mail.conf
   vi /usr/local/etc/sms_mail.conf
   ```

5. Test by hand, as `youruser`, then send an SMS such as `toto@exemple.fr Test` from the allowed number. Stop with Ctrl+C.

   ```
   python3 tools/sms_mail.py usb --config /usr/local/etc/sms_mail.conf --allow +33612345678 --sms-log /tmp/sms.log
   ```

6. The service:

   ```
   mkdir -p /usr/local/libexec/eg800q
   install -m 444 tools/eg800q.py tools/sms_mail.py /usr/local/libexec/eg800q/
   install -m 555 rc.d/sms_mail /usr/local/etc/rc.d/sms_mail
   mkdir -p /usr/local/etc/newsyslog.conf.d
   install -m 444 newsyslog/sms.conf /usr/local/etc/newsyslog.conf.d/
   sysrc sms_mail_enable=YES sms_mail_user=youruser sms_mail_args="--allow +33612345678"
   service sms_mail start
   tail -f /var/log/messages /var/log/sms
   ```

## Settings

### Service (`sysrc`)

| Variable | Default | Meaning |
|---|---|---|
| `sms_mail_enable` | `NO` | `YES` to start at boot |
| `sms_mail_user` | none, required | Unprivileged user, member of `dialer`. The service refuses to start without it or with root |
| `sms_mail_port` | `usb` | `usb` (module's USB port), `usb:3` (its other AT interface), `auto` (FTDI adapter) or a device such as `/dev/cuaU0` |
| `sms_mail_args` | empty | Options of `sms_mail.py`, at least `--allow NUMBER` |
| `sms_mail_config` | `/usr/local/etc/sms_mail.conf` | SMTP account |
| `sms_mail_sms_log` | `/var/log/sms` | SMS log, `root:dialer`, mode 660; empty for no SMS log |
| `sms_mail_dir` | `/usr/local/libexec/eg800q` | Folder of `sms_mail.py` and `eg800q.py` |
| `sms_mail_python` | `/usr/local/bin/python3` | Python interpreter |

`daemon(8)` restarts the script 5 s after any exit (module unplugged, error) and sends its output to syslog with the tag `sms_mail`.

### Script (`sms_mail.py`)

| Option | Meaning |
|---|---|
| `PORT` | Same values as `sms_mail_port` |
| `--config FILE` | SMTP account, see below (required) |
| `--allow NUMBER` | Number allowed to send mail (repeatable, required) |
| `--to ADDRESS` | Address for the SMS that do not start with one |
| `--group-wait SECONDS` | SMS from one number less than this apart are mailed together (default 5, `0` for each SMS on its own) |
| `--sms-log FILE` | Append one line per SMS received or sent |
| `--trace` | Log every byte exchanged with the module, **the SMTP password included** |

### SMTP account (`--config`)

One `key = value` per line, `#` for comments. No value may hold a `"`, since values go between quotes in the AT commands.

| Key | Meaning |
|---|---|
| `server` | SMTP server, e.g. `smtp.gmail.com` or `smtp.free.fr` (required) |
| `port` | Default 25; usually 587 for `starttls`, 465 for `ssl` |
| `security` | `none`, `ssl` or `starttls` (default) |
| `user`, `password` | Login of the account (required); for Gmail, an app password |
| `sender` | Sender address (required) |
| `sender_name` | Sender's displayed name (default: the address) |

## Behaviour

- The mail has the subject `SMS de +336...` and the SMS text as body, in UTF-8.
- The sender gets one SMS back: `Mail envoyé à toto a exemple.fr` (SMS are sent in the GSM alphabet, where `@` is replaced by ` a `), `Mail non envoyé : <reason> (<err>,<SMTP code>)`, or a reminder of the format when the SMS has no address and there is no `--to`.
- The mail is never sent twice: once handled, the SMS is deleted even if the reply SMS fails.
- The SMTP account is set again before each mail, since the module forgets it when it restarts. The password never reaches the log, even when the module refuses it (except with `--trace`).
- TLS (`ssl` or `starttls`) does not check the server certificate (`AT+QSSLCFG="seclevel"` 0): the module's clock stays in 1970 without network time, so every certificate would look not valid yet.
- Each mail uses a few KB of mobile data.
- At startup, SMS already in the module are logged then deleted without being mailed.

## Errors

The first number of `Mail non envoyé (...)` is the module's `<err>`, from Quectel's SMTP AT commands manual. The second one is the SMTP server's own code (0 if none), such as 535 for a refused login.

| `<err>` | Meaning |
|---|---|
| 652 | SMTP client busy |
| 653 | Server name not found (DNS) |
| 654 | Network error: data connection, TCP connection or sending to the server |
| 655 | Unsupported authentication type |
| 656 | Connection closed by the server |
| 657 | Data connection lost |
| 658 | Timeout |
| 659 | No recipient accepted |
| 665 | TLS failure |
| 666 | Service not available |

A Quectel forum thread reports `654,0` with SSL on firmware A10, fixed later by Quectel. If `ssl` or `starttls` fails that way, try the other one.

## Logs

| Where | What |
|---|---|
| `/var/log/messages`, tag `sms_mail` | Script log: start, each SMS, each mail and its result, battery voltage every minute |
| `/var/log/sms` | One line per SMS: `2026-10-24 17:40:12 IN  +33612345678 ...` and `... OUT ...`. Rotated monthly, 12 compressed archives |
