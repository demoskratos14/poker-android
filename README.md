# Poker - Application Android

Ce dossier contient tout le necessaire pour transformer ton moteur de
poker (Flask) en application Android (.apk), **sans avoir besoin
d'une machine Linux**.

## Comment ca marche

- `poker_engine.py` et `poker_web.py` : tes fichiers, inchanges.
- `main.py` : un petit lanceur Kivy. Au demarrage de l'app, il fait
  tourner ton serveur Flask en arriere-plan sur le telephone
  (`127.0.0.1:5000`), puis ouvre l'interface habituelle dans le
  navigateur du telephone. Tu retrouves exactement la meme interface
  que sur PC.
- `buildozer.spec` : la configuration de compilation (dependances,
  permissions, nom de l'app...).
- `.github/workflows/build.yml` : un robot qui compile l'APK
  automatiquement dans le cloud (chez GitHub), a chaque fois que tu
  envoies ton code. C'est ce qui te permet de compiler sans Linux.

## Etapes (depuis Windows, sans rien installer de lourd)

1. **Cree un compte GitHub** (gratuit) si tu n'en as pas deja un :
   https://github.com/signup

2. **Cree un nouveau depot (repository)**, par exemple nomme
   `poker-android`. Laisse-le public ou prive, peu importe.

3. **Envoie tous les fichiers de ce dossier** dans ce depot. Deux
   facons de faire, au choix :
   - Le plus simple : sur la page du depot GitHub, clique sur
     "Add file" > "Upload files", puis glisse-depose tout le contenu
     de ce dossier `pokerapp` (y compris le dossier `.github` - active
     l'affichage des fichiers/dossiers caches si besoin).
   - Ou avec Git installe sur Windows (https://git-scm.com/download/win) :
     ```
     git init
     git add .
     git commit -m "Premiere version"
     git branch -M main
     git remote add origin https://github.com/TON-PSEUDO/poker-android.git
     git push -u origin main
     ```

4. **Va dans l'onglet "Actions"** de ton depot GitHub. Une compilation
   ("Build APK") doit demarrer automatiquement. Sinon, clique sur
   "Build APK" puis sur "Run workflow".

5. **Attends la fin** (10 a 25 minutes la premiere fois : le robot
   telecharge le SDK/NDK Android). Une barre verte "cochee" apparait
   quand c'est termine.

6. **Telecharge l'APK** : clique sur la compilation terminee, puis
   tout en bas sur "poker-apk" dans la section "Artifacts". Ca
   telecharge un .zip contenant ton fichier .apk.

7. **Installe l'APK sur ton telephone Android** : transfere le .apk
   sur ton telephone (cle USB, email, Google Drive...), ouvre-le, et
   accepte l'installation depuis "source inconnue" si Android te le
   demande (c'est normal pour une app qui ne vient pas du Play Store).

## Si tu modifies le code plus tard

Il suffit de renvoyer les fichiers modifies sur GitHub (nouveau
"commit" / nouveau push) : le robot recompile automatiquement un
nouvel APK a chaque envoi.

## Comment s'affiche l'interface

Cette version utilise une **WebView Android native** (le meme
composant que Chrome utilise en interne) integree directement dans
l'application : pas de navigateur externe, pas de barre d'adresse.
L'utilisateur ne voit que la table de poker, comme une app classique.
Le jeu (`poker_engine.py` + `poker_web.py`) tourne entierement sur le
telephone, sans connexion internet.

Le bouton "Retour" physique du telephone se comporte comme dans un
navigateur : il revient a la page precedente de la table de poker
tant qu'il y a un historique de navigation dans l'app, et ferme
l'application seulement quand il n'y en a plus (sur l'ecran
d'accueil, par exemple).

## Faire jouer les IA automatiquement (sans copier-coller)

Le dossier `relay/` contient un petit serveur a lancer sur ton PC : il
garde une conversation ouverte avec Claude, ChatGPT et Gemini (leurs
versions web gratuites) et fait le copier-coller a ta place, quand
c'est le tour d'une IA a la table.

Marche a suivre :
1. Suis les instructions dans `relay/README.md` pour installer et
   lancer le relais sur ton PC.
2. Dans l'app Android, ouvre "Configurer le relais IA (PC)" (visible
   sur l'ecran de la table) et renseigne l'adresse affichee par le
   relais au demarrage.
3. Un bouton "Faire jouer les IA" apparait sur l'ecran de la table :
   il fait agir automatiquement, a la suite, tous les bots dont c'est
   le tour, jusqu'a ce que ce soit a toi de jouer.

**A savoir :** ce systeme pilote un navigateur comme le ferait un
humain (il ne passe pas par une API officielle) - c'est ce qui permet
de rester sur les comptes gratuits, mais ca reste plus fragile qu'une
vraie API (voir les avertissements detailles dans `relay/README.md`).
Le bloc de collage manuel reste disponible en secours si une IA ne
repond pas dans un format exploitable.

## Limites de cette version "sommaire"

- Le serveur ne tourne que pendant que l'app est ouverte au premier
  plan (comportement normal d'une app Android en arriere-plan).
- Les parties sauvegardees (`poker_games/`) restent uniquement sur le
  telephone utilise.
