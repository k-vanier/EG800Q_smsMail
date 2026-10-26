#!/usr/bin/env python3
# Turns SMS into e-mails: the module itself sends them, with its own SMTP
# client (AT+QSMTP...) over its mobile data connection.
# The SMS "toto@exemple.fr Je rentre à 19h" mails "Je rentre à 19h" to
# toto@exemple.fr; with --to, an SMS that does not start with an address is
# mailed to that address. The sender gets an SMS back: mail sent, or why not.
# Only the --allow numbers can send mail; SMS from anyone else are ignored.
#
# Never runs next to another program that reads the module's SMS (such as
# sms_claude.py from EG800Q_smsResponder): both would read and delete the
# same SMS. Every SMS is deleted once handled.
#
# Usage: python3 sms_mail.py PORT --config FILE --allow +336... [--to ADDRESS]
#                            [--group-wait SECONDS] [--sms-log FILE] [--trace]
#   PORT: usb (module's USB port), auto (FTDI adapter), /dev/cuaU0, /dev/ttyUSB0
# FILE holds the SMTP account, one "key = value" per line (# for comments),
# readable by the service user only since it holds the password:
#   server = smtp.free.fr
#   port = 587
#   security = starttls          none, ssl or starttls
#   user = login
#   password = ...
#   sender = moi@free.fr
#   sender_name = EG800Q         optional, default: the sender address
# Needs pyserial, and pyusb for the "usb" port.

import argparse
import re

from eg800q import Modem, ModemError, is_phone_number, log

PDP_CONTEXT = 1  # data context of the module, with the operator's APN (AT+QICSGP=1)
SSL_CONTEXT = 1
ACTIVATE_TIMEOUT = 150  # seconds; Quectel's maximum response time of AT+QIACT
BODY_INPUT_TIME = 30  # seconds the module waits for the body after CONNECT
BODY_MAX = 10240  # bytes, the module's limit for a mail body
MAIL_TIMEOUT = 120  # seconds given to AT+QSMTPPUT to send the mail
SECURITY = {'none': 0, 'ssl': 1, 'starttls': 2}  # AT+QSMTPCFG="ssltype"
CONFIG_KEYS = ('server', 'port', 'security', 'user', 'password', 'sender', 'sender_name')
ADDRESS = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+', re.ASCII)
SUBJECT = 'SMS de %s'
# <err> of +QSMTPPUT: <err>,<protocol_error>, from Quectel's SMTP AT commands manual
SMTP_ERRORS = {
	651: 'erreur inconnue',
	652: 'serveur occupé',
	653: 'serveur introuvable (DNS)',
	654: 'erreur réseau',
	655: "type d'authentification non supporté",
	656: 'connexion fermée par le serveur',
	657: 'données mobiles coupées',
	658: 'délai dépassé',
	659: 'destinataire refusé',
	660: "échec de l'envoi",
	664: 'paramètre incorrect',
	665: 'échec SSL',
	666: 'service indisponible',
}
USAGE_REPLY = "Commencez le SMS par l'adresse mail du destinataire, puis le message."
EMPTY_REPLY = "Mail non envoyé : le message est vide, écrivez-le après l'adresse."


def read_config(path):
	"""Returns the SMTP account of FILE as a dict, or raises ValueError."""
	config = {'port': '25', 'security': 'starttls'}
	with open(path, encoding='utf-8') as f:
		for number, line in enumerate(f, 1):
			line = line.strip()
			if not line or line.startswith('#'):
				continue
			key, sep, value = line.partition('=')
			key, value = key.strip().lower(), value.strip()
			if not sep or key not in CONFIG_KEYS:
				raise ValueError('%s line %d: expected one of %s = value' % (path, number, ', '.join(CONFIG_KEYS)))
			# Values go between double quotes in the AT commands
			if '"' in value:
				raise ValueError('%s line %d: the module cannot take a " in %s' % (path, number, key))
			config[key] = value
	missing = [key for key in ('server', 'user', 'password', 'sender') if not config.get(key)]
	if missing:
		raise ValueError('%s: missing %s' % (path, ', '.join(missing)))
	if config['security'] not in SECURITY:
		raise ValueError('%s: security must be none, ssl or starttls' % path)
	if not config['port'].isdigit():
		raise ValueError('%s: port must be a number' % path)
	config.setdefault('sender_name', config['sender'])
	return config


def activate_data(modem):
	"""Opens the module's data connection unless it is already open."""
	for line in modem.command('AT+QIACT?'):
		# +QIACT: 1,1,1,"10.107.17.191": context 1, activated
		if re.match(r'\s*\+QIACT: %d,1,' % PDP_CONTEXT, line):
			return
	log('opening the data connection (context %d)' % PDP_CONTEXT)
	modem.command('AT+QIACT=%d' % PDP_CONTEXT, ACTIVATE_TIMEOUT)


def configure_smtp(modem, config):
	"""Sets the SMTP account; done before each mail, since the module forgets
	it when it restarts."""
	for cmd in ('AT+QSMTPCFG="contextid",%d' % PDP_CONTEXT,
			'AT+QSMTPCFG="ssltype",%d' % SECURITY[config['security']],
			'AT+QSMTPCFG="sslctxid",%d' % SSL_CONTEXT,
			'AT+QSSLCFG="sslversion",%d,4' % SSL_CONTEXT,  # 4: any TLS version
			'AT+QSSLCFG="ciphersuite",%d,0xFFFF' % SSL_CONTEXT,  # any cipher suite
			# 0: the server certificate is not checked. The module's clock is not
			# set (it stays in 1970 without NITZ), so every certificate would
			# look not valid yet.
			'AT+QSSLCFG="seclevel",%d,0' % SSL_CONTEXT,
			'AT+QSMTPCFG="smtpserver","%s",%s' % (config['server'], config['port']),
			'AT+QSMTPCFG="sender","%s","%s"' % (config['sender_name'], config['sender'])):
		modem.command(cmd)
	try:
		modem.command('AT+QSMTPCFG="account","%s","%s"' % (config['user'], config['password']))
	except ModemError as e:
		# The error message holds the command, so the password: never log it
		raise ModemError('AT+QSMTPCFG="account" -> %s' % str(e).split(' -> ', 1)[-1]) from None


def send_mail(modem, config, to, subject, body):
	"""Sends the mail through the module; returns (err, protocol_error) of
	+QSMTPPUT, (0, 0) when it was sent."""
	activate_data(modem)
	configure_smtp(modem, config)
	modem.command('AT+QSMTPCLR')
	modem.command('AT+QSMTPDST=1,1,"%s"' % to)
	# Charset 1 (UTF-8): the subject goes as a hex string, the body as is
	modem.command('AT+QSMTPSUB=1,"%s"' % subject.encode('utf-8').hex().upper())
	data = body.encode('utf-8')[:BODY_MAX]
	modem.command_data('AT+QSMTPBODY=1,%d,%d' % (len(data), BODY_INPUT_TIME), data, BODY_INPUT_TIME + 10)
	modem.command('AT+QSMTPPUT=%d' % MAIL_TIMEOUT)
	line = modem.wait_urc('+QSMTPPUT:', MAIL_TIMEOUT + 30)
	m = re.match(r'\+QSMTPPUT: (\d+),(\d+)', line)
	if not m:
		raise ModemError('garbled result %r' % line)
	return int(m.group(1)), int(m.group(2))


def parse_sms(text, default_to):
	"""Returns (address, body) of an SMS, address None if it has none."""
	m = ADDRESS.match(text.strip())
	if m:
		return m.group(0), text.strip()[m.end():].strip(' :\n')
	return default_to, text.strip()


def main():
	parser = argparse.ArgumentParser(description='Mails the SMS received by the EG800Q-EU, through its own SMTP client.')
	parser.add_argument('port', help='usb for the own USB port of the module, or the serial port of the FTDI adapter (/dev/cuaU1, /dev/ttyUSB0, COM18), or auto to find it')
	parser.add_argument('--config', required=True, metavar='FILE', help='SMTP account: server, port, security, user, password, sender')
	parser.add_argument('--allow', action='append', required=True, metavar='NUMBER',
		help='number allowed to send mail (repeatable, e.g. +33612345678)')
	parser.add_argument('--to', metavar='ADDRESS', help='address for the SMS that do not start with one')
	parser.add_argument('--group-wait', type=float, default=5, metavar='SECONDS',
		help='SMS from one number arriving less than this apart are mailed together (default: 5, 0 to mail each one)')
	parser.add_argument('--trace', action='store_true', help='log every byte exchanged with the module (the password too)')
	parser.add_argument('--sms-log', metavar='FILE',
		help='append one line per SMS received or sent to this file, e.g. /var/log/sms')
	args = parser.parse_args()
	if args.to and not ADDRESS.fullmatch(args.to):
		parser.error('--to: not a mail address: %s' % args.to)
	try:
		config = read_config(args.config)
	except (OSError, ValueError) as e:
		parser.error(str(e))

	modem = Modem(args.port, args.trace, args.sms_log)
	log('purged %d SMS already in memory' % modem.purge_sms())

	def reply(sender, text):
		# The mail is already handled: a failed reply must not make serve()
		# handle the SMS again, which would send the mail twice
		try:
			modem.send_sms(sender, text)
			log('replied to %s: %s' % (sender, text))
		except ModemError as e:
			log('error: cannot reply to %s: %s' % (sender, e))

	def on_sms(sender, text):
		if not is_phone_number(sender) or sender not in args.allow:
			log('ignored, %s is not in --allow' % sender)
			return
		to, body = parse_sms(text, args.to)
		if not to:
			reply(sender, USAGE_REPLY)
			return
		if not body:
			reply(sender, EMPTY_REPLY)
			return
		log('mailing %s to %s: %r' % (sender, to, body))
		try:
			err, protocol = send_mail(modem, config, to, SUBJECT % sender, body)
		except ModemError as e:
			log('error: %s' % e)
			reply(sender, 'Mail non envoyé : le module a refusé la commande.')
			return
		if err == 0:
			log('mail sent to %s' % to)
			reply(sender, 'Mail envoyé à %s' % to)
			return
		reason = SMTP_ERRORS.get(err, 'erreur %d' % err)
		log('mail to %s failed: %d,%d (%s)' % (to, err, protocol, reason))
		reply(sender, 'Mail non envoyé : %s (%d,%d).' % (reason, err, protocol))

	log('ready on %s, mailing SMS from %s' % (modem.port, ', '.join(args.allow)))
	modem.serve(on_sms, args.group_wait)


if __name__ == '__main__':
	try:
		main()
	except KeyboardInterrupt:
		pass
