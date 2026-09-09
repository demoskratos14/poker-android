[app]

# (str) Titre de l'application, tel qu'affiche sous l'icone
title = Poker

# (str) Nom du package (technique, sans espaces ni accents)
package.name = pokerapp

# (str) Domaine du package (peut rester generique)
package.domain = org.exemple

# (str) Dossier source de l'application (celui contenant main.py)
source.dir = .

# (list) Extensions de fichiers a inclure dans l'APK
source.include_exts = py,json,txt,md

# (list) Dossiers a NE PAS inclure dans l'APK : le relais tourne sur
# le PC, pas sur le telephone, et n'a donc rien a faire dans l'APK
# (ses dependances, comme Playwright, ne sont d'ailleurs pas
# compatibles Android). On exclut aussi le workflow GitHub et les
# parties deja sauvegardees en local.
source.exclude_dirs = relay,.github,poker_games,__pycache__

# (str) Version affichee de l'application
version = 0.1

# (list) Dependances Python. python3 + kivy pour l'interface de
# lancement, flask pour le serveur web, pyjnius pour ouvrir le
# navigateur Android depuis Python.
requirements = python3,kivy,flask,pyjnius,werkzeug,jinja2,markupsafe,itsdangerous,click,blinker

# (str) Orientation de l'ecran : portrait, landscape, ou all
orientation = portrait

# (bool) Application plein ecran (0 = non, garde la barre de statut)
fullscreen = 0

# (list) Permissions Android necessaires.
# INTERNET est indispensable meme pour un serveur en 127.0.0.1.
android.permissions = INTERNET
android.extra_manifest_application_arguments = %(source.dir)s/extra_manifest_application_arguments.txt

# (int) API Android cible (laisser les valeurs par defaut recentes de Buildozer)
#android.api = 33

# (int) API Android minimum supportee
android.minapi = 21

# (str) Architectures visees (arm64-v8a couvre l'immense majorite des
# telephones actuels ; ajouter armeabi-v7a augmente la compatibilite
# avec de tres vieux appareils mais allonge la compilation)
android.archs = arm64-v8a

# (bool) Accepter automatiquement les licences du SDK Android
android.accept_sdk_license = True

# (str) Bootstrap python-for-android a utiliser
p4a.bootstrap = sdl2


[buildozer]

# (int) Niveau de log (0 = erreurs seulement, 1 = info, 2 = debug)
log_level = 2

# (int) Afficher un avertissement si build lance en root (0 = non recommande de desactiver)
warn_on_root = 1
