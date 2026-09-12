#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
INTERFACE WEB - Moteur de Poker
================================
A lancer avec Pydroid 3, en gardant poker_engine.py dans le MEME dossier.

Installation (une seule fois) :
    pip install flask

Lancement :
    Executez ce fichier (bouton Play dans Pydroid 3).
    Puis ouvrez votre navigateur sur : http://127.0.0.1:5000

L'etat de la partie est partage avec poker_engine.py (meme fichier
poker_state.json) : vous pouvez utiliser l'un ou l'autre indifferemment.
"""

import io
import math
import os
import random
import time
import contextlib
import html
import json as json_module
import threading
import urllib.request
import urllib.error
from flask import Flask, request, redirect, url_for

from poker_engine import (
    Game, CONTROLLER_SHORTCUTS, DEFAULT_NAME_POOL,
    build_default_name_pool,
    SUITS, RANK_NAMES, best_hand_with_cards, describe,
    order_cards_for_display,
)

# Structures de tournoi predefinies, inspirees de vrais circuits de poker.
# hands_per_level remplace la notion de "minutes par niveau" (impossible a
# reproduire ici) : les blinds/antes doublent toutes les N mains.
TOURNAMENT_PRESETS = {
    "wsop": {
        "label": "Lent (style WSOP Main Event)",
        "description": (
            "Tapis tres profond (300 grosses blindes), niveaux tres longs. "
            "Le vrai WSOP Main Event demarre a 60 000 jetons avec des blindes "
            "100/200 et des niveaux de 2h : un format qui recompense la patience."
        ),
        "stack": 60000, "small_blind": 100, "big_blind": 200, "ante": 25,
        "hands_per_level": 20,
    },
    "ept": {
        "label": "Modere (style EPT / circuits majeurs)",
        "description": (
            "Tapis profond (environ 150 grosses blindes), rythme intermediaire "
            "typique des grands circuits comme l'European Poker Tour ou le WPT."
        ),
        "stack": 30000, "small_blind": 100, "big_blind": 200, "ante": 25,
        "hands_per_level": 12,
    },
    "turbo": {
        "label": "Rapide (turbo)",
        "description": (
            "Tapis court (environ 50 grosses blindes) et blindes qui montent "
            "vite : parties courtes et agressives, ideal pour une session rapide."
        ),
        "stack": 10000, "small_blind": 100, "big_blind": 200, "ante": 10,
        "hands_per_level": 6,
    },
    "custom": {
        "label": "Personnalise",
        "description": "Choisissez vous-meme tous les reglages ci-dessous.",
        "stack": 50000, "small_blind": 100, "big_blind": 200, "ante": 10,
        "hands_per_level": 10,
    },
}

app = Flask(__name__)

# ----------------------------------------------------------------------
# Gestion multi-parties : chaque partie a son propre fichier de sauvegarde,
# un petit registre garde la liste des parties + laquelle est active.
# ----------------------------------------------------------------------

GAMES_DIR = "poker_games"
LEGACY_SAVE_FILE = "poker_state.json"  # ancien fichier unique (avant les onglets)
_GAME_CACHE = {}  # gid -> instance Game deja chargee (evite de relire le disque a chaque requete)


def _ensure_games_dir():
    os.makedirs(GAMES_DIR, exist_ok=True)


# ----------------------------------------------------------------------
# Liste PERSISTANTE des types d'IA proposes a la creation d'une partie
# (Claude/Vibe/ChatGPT par defaut). Ajouter ou supprimer une IA ici est
# definitif : ca modifie la liste proposee pour TOUTES les parties futures,
# contrairement au champ "+ Nouvelle IA..." (ponctuel, un seul siege).
# ----------------------------------------------------------------------

AI_TYPES_PATH = os.path.join(GAMES_DIR, "_ai_types.json")
DEFAULT_AI_TYPES = ["Claude", "Vibe", "ChatGPT"]


def load_ai_types():
    _ensure_games_dir()
    if os.path.exists(AI_TYPES_PATH):
        try:
            with open(AI_TYPES_PATH, "r", encoding="utf-8") as f:
                types = json_module.load(f)
            if isinstance(types, list) and types:
                return types
        except (json_module.JSONDecodeError, OSError, UnicodeDecodeError):
            pass
    return list(DEFAULT_AI_TYPES)


def save_ai_types(types):
    _ensure_games_dir()
    with open(AI_TYPES_PATH, "w", encoding="utf-8") as f:
        json_module.dump(types, f, ensure_ascii=False, indent=2)


def add_ai_type(name):
    name = name.strip()
    if not name:
        return
    types = load_ai_types()
    if name not in types:
        types.append(name)
        save_ai_types(types)


def remove_ai_type(name):
    types = load_ai_types()
    types = [t for t in types if t != name]
    if not types:
        types = list(DEFAULT_AI_TYPES)  # ne jamais rester sans aucune IA proposee
    save_ai_types(types)


# ----------------------------------------------------------------------
# Relais PC : au lieu de copier-coller manuellement le resume de main
# dans chaque IA, l'app peut interroger un petit serveur qui tourne sur
# le PC de l'utilisateur (voir dossier relay/). Ce serveur garde des
# conversations ouvertes (navigateur) avec chaque IA et fait le
# copier-coller a sa place. L'adresse de ce serveur (ex: son IP locale
# sur le meme Wi-Fi) est enregistree ici.
# ----------------------------------------------------------------------

RELAY_CONFIG_PATH = os.path.join(GAMES_DIR, "_relay_config.json")


def load_relay_config():
    _ensure_games_dir()
    if os.path.exists(RELAY_CONFIG_PATH):
        try:
            with open(RELAY_CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json_module.load(f)
            if isinstance(cfg, dict):
                return cfg
        except (json_module.JSONDecodeError, OSError, UnicodeDecodeError):
            pass
    return {"relay_url": ""}


def save_relay_config(cfg):
    _ensure_games_dir()
    with open(RELAY_CONFIG_PATH, "w", encoding="utf-8") as f:
        json_module.dump(cfg, f, ensure_ascii=False, indent=2)


def ask_relay_for_action(ai_type, message, timeout=90):
    """Envoie le resume de main au relais PC pour le compte de l'IA
    `ai_type`, et retourne (reponse_brute, erreur). En cas de succes,
    erreur est None. En cas d'echec (relais non configure, injoignable,
    IA non geree, etc.), reponse_brute est None et erreur decrit le
    probleme pour affichage a l'utilisateur.

    Met egalement a jour RELAY_STATUS[ai_type] ("waiting" pendant
    l'appel, puis "ok"/"error" selon le resultat), pour le voyant
    affiche a cote du nom de l'IA sur la table."""
    cfg = load_relay_config()
    relay_url = (cfg.get("relay_url") or "").rstrip("/")
    if not relay_url:
        return None, "Aucun relais PC configure. Va dans 'Configurer le relais IA' pour renseigner son adresse."

    _set_relay_status(ai_type, "waiting")

    payload = json_module.dumps({"ai": ai_type, "message": message}).encode("utf-8")
    req = urllib.request.Request(
        f"{relay_url}/ask",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json_module.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # Le relais a repondu avec un code d'erreur (ex: 500) : on lit
        # son corps de reponse JSON pour afficher le VRAI message
        # explicatif (ex: "aucun des selecteurs connus n'a fonctionne")
        # plutot que le generique "HTTP Error 500" peu utile.
        try:
            detail = json_module.loads(e.read().decode("utf-8")).get("error", str(e))
        except (json_module.JSONDecodeError, ValueError, UnicodeDecodeError, AttributeError):
            detail = str(e)
        _set_relay_status(ai_type, "error")
        return None, f"Le relais PC a renvoye une erreur pour {ai_type} : {detail}"
    except urllib.error.URLError as e:
        _set_relay_status(ai_type, "error")
        return None, f"Impossible de joindre le relais PC ({relay_url}) : {e}"
    except (TimeoutError, OSError) as e:
        _set_relay_status(ai_type, "error")
        return None, f"Le relais PC n'a pas repondu a temps : {e}"
    except (json_module.JSONDecodeError, ValueError) as e:
        _set_relay_status(ai_type, "error")
        return None, f"Reponse invalide du relais PC : {e}"

    if "error" in body:
        _set_relay_status(ai_type, "error")
        return None, f"Le relais PC a renvoye une erreur pour {ai_type} : {body['error']}"
    reply = body.get("reply")
    if not reply:
        _set_relay_status(ai_type, "error")
        return None, f"Le relais PC n'a renvoye aucun texte pour {ai_type}."
    _set_relay_status(ai_type, "ok")
    return reply, None


def _relay_broadcast(messages, label):
    """Envoie un message different a chaque IA (dict {controleur: texte})
    via le relais PC, de facon synchrone (le relais ne gere qu'une requete
    Playwright a la fois de toute facon - voir relay_server.py). Ne fait
    rien si aucun relais n'est configure (retourne None) : ca reste donc
    sans effet pour qui n'utilise pas le relais et continue au copier-coller
    manuel. Sinon, retourne un texte de journal resumant chaque envoi, a
    ajouter a LAST_LOG pour que l'utilisateur voie ce qui a ete transmis et
    si une IA n'a pas ete jointe."""
    cfg = load_relay_config()
    if not (cfg.get("relay_url") or "").strip():
        return None
    log_parts = []
    for ai_type, text in messages.items():
        reply, error = ask_relay_for_action(ai_type, text)
        if error:
            log_parts.append(f"[{label} -> {ai_type}] ERREUR : {error}")
        else:
            log_parts.append(f"[{label} -> {ai_type}] envoye avec succes.")
    return "\n".join(log_parts)


_relay_log_lock = threading.Lock()


def _relay_broadcast_async(messages, label):
    """Version non-bloquante de _relay_broadcast : lance l'envoi dans un
    thread separe et revient tout de suite, sans faire attendre
    l'utilisateur (creation de partie, nouvelle main...) le temps que le
    relais PC tape et attende la reponse de chaque IA - ce qui peut prendre
    jusqu'a 90s par IA, voire echouer/trainer si le relais n'est pas
    joignable. Le resultat est ajoute a LAST_LOG des qu'il est connu (donc
    visible au prochain rafraichissement de la page), sans jamais bloquer
    l'action en cours (creer la partie, distribuer une main...)."""
    def _worker():
        log = _relay_broadcast(messages, label)
        if log is None:
            return
        global LAST_LOG
        with _relay_log_lock:
            LAST_LOG = (LAST_LOG + "\n\n" + log) if LAST_LOG else log

    threading.Thread(target=_worker, daemon=True).start()


def _index_path():
    return os.path.join(GAMES_DIR, "_index.json")


def _rebuild_index_from_disk():
    """Reconstruit un registre minimal a partir des fichiers de parties
    presents dans GAMES_DIR, utilise quand _index.json est illisible/corrompu.
    Chaque partie vit dans son PROPRE fichier ({gid}.json, voir _game_file) :
    perdre _index.json ne perd donc pas les parties elles-memes, seulement
    la liste/l'ordre/le nom affiche et la notion de "partie active", que
    l'on peut retrouver en listant simplement les fichiers presents."""
    _ensure_games_dir()
    games = []
    for fname in sorted(os.listdir(GAMES_DIR)):
        if fname == "_index.json" or not fname.endswith(".json"):
            continue
        gid = fname[:-len(".json")]
        games.append({"id": gid, "name": f"Partie recuperee ({gid})"})
    return {"games": games, "active": (games[0]["id"] if games else None)}


def _load_index():
    _ensure_games_dir()
    path = _index_path()
    if not os.path.exists(path):
        return {"games": [], "active": None}
    try:
        with open(path, "r", encoding="utf-8") as f:
            idx = json_module.load(f)
        if not isinstance(idx, dict) or "games" not in idx:
            raise ValueError("structure du registre invalide (cle 'games' absente)")
        return idx
    except (json_module.JSONDecodeError, OSError, UnicodeDecodeError, ValueError) as e:
        # Un _index.json corrompu (arret brutal, disque plein...) ne doit
        # jamais faire planter l'application web sur CHAQUE requete. On met
        # le fichier fautif de cote pour recuperation manuelle eventuelle,
        # puis on reconstruit un registre minimal en scannant les fichiers
        # de parties encore presents sur le disque : aucune partie n'est
        # perdue, seuls les noms personnalises et l'ordre d'affichage le
        # sont eventuellement.
        backup_path = f"{path}.corrompu.{int(time.time())}"
        try:
            os.replace(path, backup_path)
            hint = f"une copie a ete conservee sous : {backup_path}"
        except OSError:
            hint = "impossible de conserver une copie du fichier fautif"
        print(f"\n/!\\ ATTENTION : registre des parties illisible/corrompu ({e}).")
        print(f"    {hint}")
        rebuilt = _rebuild_index_from_disk()
        print(f"    Reconstruction automatique depuis les fichiers de parties presents sur "
              f"disque : {len(rebuilt['games'])} partie(s) retrouvee(s).\n")
        _save_index(rebuilt)
        return rebuilt


def _save_index(idx):
    _ensure_games_dir()
    # Ecriture atomique (fichier temporaire + os.replace) : evite qu'une
    # interruption pendant l'ecriture ne laisse _index.json tronque/corrompu,
    # meme pattern que pour la sauvegarde de chaque partie (Game.save()).
    path = _index_path()
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json_module.dump(idx, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def _game_file(gid):
    return os.path.join(GAMES_DIR, f"{gid}.json")


def _get_game_instance(gid):
    if gid not in _GAME_CACHE:
        g = Game(save_file=_game_file(gid))
        g.load()
        _GAME_CACHE[gid] = g
    return _GAME_CACHE[gid]


def create_game(name, migrate_from=None, tournament_id=None):
    """Cree une nouvelle partie (avec son propre fichier), l'enregistre dans
    le registre et la rend active. Si migrate_from est fourni (utilise une
    seule fois, pour recuperer l'ancien fichier unique poker_state.json),
    la partie existante y est recopiee au lieu de partir de zero.

    tournament_id : si fourni, regroupe cette partie avec toutes les autres
    parties portant le meme identifiant (utilise pour les tournois
    multi-table, afin de savoir quelles tables sont "annexes" les unes des
    autres et synchroniser leurs eliminations - voir spawn_extra_tables et
    _sync_satellite_eliminations)."""
    idx = _load_index()
    gid = f"partie_{len(idx['games']) + 1}_{int(time.time())}"
    entry = {"id": gid, "name": name}
    if tournament_id:
        entry["tournament_id"] = tournament_id
    idx["games"].append(entry)
    idx["active"] = gid
    _save_index(idx)

    if migrate_from and os.path.exists(migrate_from):
        g = Game(save_file=migrate_from)
        g.load()
        g.save_file = _game_file(gid)
        g.save()
    else:
        g = Game(save_file=_game_file(gid))
        g.save()
    _GAME_CACHE[gid] = g
    return gid


def _assign_balanced_ai_controllers(seat_controllers, auto_indices):
    """Remplace en place, dans seat_controllers, les sieges marques a None
    (aucun role explicitement choisi par l'utilisateur pour ce siege -
    controleur "Auto" laisse tel quel dans le formulaire) par une IA
    choisie aleatoirement, tout en equilibrant au mieux le nombre de
    sieges attribues a chaque IA.

    Le pool d'IA utilise pour cet equilibrage est celui des IA deja
    explicitement choisies pour d'AUTRES sieges de la meme table (dans
    leurs proportions d'origine - le compte de depart de chaque IA tient
    compte de ces choix explicites) : les sieges "Auto" viennent ainsi
    completer/equilibrer la repartition plutot que l'ignorer. Si aucune
    IA n'a ete explicitement choisie nulle part sur la table (tous les
    sieges sont "Auto", ou tous sont "Humain"), on retombe sur les 3 IA
    par defaut (Claude/Vibe/ChatGPT).

    A chaque siege "Auto" a pourvoir (dans un ordre tire au sort), l'IA
    choisie est tiree au hasard PARMI celles ayant actuellement le moins
    de sieges deja attribues (egalite departagee au hasard) : le resultat
    reste aleatoire tout en gardant les compteurs aussi equilibres que
    possible au fil de l'attribution."""
    if not auto_indices:
        return
    explicit_ai = [
        CONTROLLER_SHORTCUTS.get(c, c) for c in seat_controllers
        if c is not None and CONTROLLER_SHORTCUTS.get(c, c).lower() != "humain"
    ]
    ai_pool = sorted(set(explicit_ai)) if explicit_ai else load_ai_types()
    counts = {a: explicit_ai.count(a) for a in ai_pool}
    order = list(auto_indices)
    random.shuffle(order)
    for i in order:
        min_count = min(counts.values())
        candidates = [a for a in ai_pool if counts[a] == min_count]
        chosen = random.choice(candidates)
        counts[chosen] += 1
        seat_controllers[i] = chosen


def _is_satellite_table(gm):
    """Renvoie True si cette entree du registre est une table ANNEXE d'un
    tournoi multi-table (IA uniquement, generee par spawn_extra_tables),
    par opposition a la table principale (celle du joueur humain) ou a une
    partie autonome hors tournoi.

    La table principale se voit attribuer, au moment du passage en
    multi-table, un tournament_id egal a son PROPRE id (voir
    spawn_extra_tables) ; les tables annexes, elles, ont un tournament_id
    qui pointe vers cet id principal mais different du leur. C'est ce qui
    permet de les distinguer sans ambiguite."""
    tid = gm.get("tournament_id")
    return bool(tid) and tid != gm["id"]


def _purge_orphaned_satellites():
    """Nettoie les tables ANNEXES devenues orphelines : celles dont le
    tournament_id ne correspond plus a AUCUNE table principale presente
    dans le registre (parce que cette table principale a ete supprimee -
    y compris via une ancienne version du code qui ne retirait pas encore
    ses annexes en meme temps - ou parce qu'un equilibrage les a fermees
    et qu'elles ne servent plus a rien).

    Sans ce nettoyage, ces entrees orphelines restent indefiniment dans
    _index.json (avec leur fichier .json correspondant) : invisibles dans
    les onglets (voir render_game_tabs), mais toujours candidates pour
    devenir "active" par defaut - c'est exactement ce qui cree une
    "partie fantome" quand on supprime sa derniere vraie partie.

    Appelee a chaque chargement de page (avant get_active_game), donc les
    orphelins deja presents sont purges automatiquement, sans manipulation
    de l'utilisateur."""
    idx = _load_index()
    known_ids = {gm["id"] for gm in idx["games"]}
    orphans = [
        gm for gm in idx["games"]
        if _is_satellite_table(gm) and gm.get("tournament_id") not in known_ids
    ]
    if not orphans:
        return
    orphan_ids = {gm["id"] for gm in orphans}
    idx["games"] = [gm for gm in idx["games"] if gm["id"] not in orphan_ids]
    if idx.get("active") in orphan_ids:
        idx["active"] = None
    _save_index(idx)
    for oid in orphan_ids:
        _GAME_CACHE.pop(oid, None)
        try:
            os.remove(_game_file(oid))
        except OSError:
            pass


def get_active_game():
    """Retourne l'instance Game de la partie actuellement active, en creant
    une premiere partie si le registre ne contient plus aucune VRAIE partie
    (avec recuperation automatique de l'ancien fichier unique s'il existe,
    pour ne pas perdre une partie en cours lors de la mise a jour vers le
    systeme d'onglets).

    Ne choisit jamais une table ANNEXE de tournoi comme partie active : ces
    tables ne sont pas navigables/jouables directement (voir
    _is_satellite_table), donc si le registre ne contient plus que ce genre
    de tables (orphelines ou non), on considere qu'il n'y a plus de partie
    du tout et on en recree une, exactement comme si le registre etait
    vide."""
    _purge_orphaned_satellites()
    idx = _load_index()
    real_games = [gm for gm in idx["games"] if not _is_satellite_table(gm)]
    if not real_games:
        gid = create_game("Partie 1", migrate_from=LEGACY_SAVE_FILE)
        return _get_game_instance(gid)
    active = idx.get("active")
    if not active or not any(gm["id"] == active for gm in real_games):
        active = real_games[0]["id"]
        idx["active"] = active
        _save_index(idx)
    return _get_game_instance(active)


def list_games():
    return _load_index()["games"]


def set_active_game(gid):
    idx = _load_index()
    if any(gm["id"] == gid for gm in idx["games"]):
        idx["active"] = gid
        _save_index(idx)
        return True
    return False


def spawn_extra_tables(num_tables, n_players, stack, name_pool_raw, main_seat_controllers,
                        small_blind, big_blind, ante, hands_per_level,
                        shared_name_pool=None):
    """Cree automatiquement (num_tables - 1) tables supplementaires pour un
    tournoi multi-table : meme nombre de joueurs et meme structure (tapis,
    blindes, ante, niveaux) que la table principale ("ma table").

    Les sieges de ces tables supplementaires sont repartis automatiquement
    entre les MEMES TYPES D'IA que ceux presents a la table principale (dans
    les memes proportions, en cyclant sur la liste si besoin). Le ou les
    sieges humains de la table principale sont remplaces par une de ces IA
    sur les autres tables, puisqu'un humain ne peut pas jouer simultanement
    sur plusieurs tables dans cette application.

    shared_name_pool : pool de noms unique pour tout le tournoi (deja
    partiellement consomme par la table principale), continue d'etre
    consomme table par table ici afin qu'aucun nom ne soit distribue deux
    fois sur l'ensemble du tournoi. Si non fourni (retro-compatibilite),
    chaque table reconstruit son propre pool comme avant."""
    idx = _load_index()
    main_gid = idx["active"]
    main_name = next((gm["name"] for gm in idx["games"] if gm["id"] == main_gid), "Partie")

    # Pool des types de controleurs IA presents a la table principale (le
    # controleur humain en est exclu). L'ordre/les proportions d'origine
    # sont conserves : si 2 sieges sur 6 etaient geres par "Vibe", le pool
    # comptera bien 2 fois "Vibe", pour respecter au mieux la meme
    # repartition sur les autres tables.
    ai_pool = [c for c in main_seat_controllers
               if CONTROLLER_SHORTCUTS.get(c, c).lower() != "humain"]
    if not ai_pool:
        ai_pool = ["1"]  # repli : Claude par defaut si la table principale n'a aucune IA

    # Renomme la table principale pour bien identifier le tournoi multi-table,
    # et lui attribue un tournament_id (son propre gid) : c'est cet
    # identifiant qui sera partage par toutes les tables annexes creees
    # ci-dessous, pour pouvoir les retrouver et synchroniser leurs
    # eliminations (voir _sync_satellite_eliminations).
    for gm in idx["games"]:
        if gm["id"] == main_gid:
            gm["name"] = f"{main_name} - Table 1"
            gm["tournament_id"] = main_gid
    _save_index(idx)

    for t in range(2, num_tables + 1):
        gid = create_game(f"{main_name} - Table {t}", tournament_id=main_gid)
        # Cycle sur le pool d'IA pour remplir tous les sieges de cette table,
        # puis melange l'ordre des sieges pour ne pas reproduire exactement
        # la meme disposition sur chaque table.
        extra_controllers = [ai_pool[i % len(ai_pool)] for i in range(n_players)]
        random.shuffle(extra_controllers)
        g = _get_game_instance(gid)
        g.setup_players_web(
            stack, name_pool_raw, extra_controllers, {},
            small_blind=small_blind, big_blind=big_blind, ante=ante,
            hands_per_level=hands_per_level,
            shared_name_pool=shared_name_pool,
        )

    set_active_game(main_gid)


def _sync_satellite_eliminations(n_eliminations):
    """A appeler juste apres qu'un showdown ait elimine n_eliminations
    joueur(s) sur la table active (la table ou vous jouez, avec un siege
    humain). Retrouve toutes les tables ANNEXES du meme tournoi multi-table
    (memes tournament_id, uniquement des sieges IA - donc sur lesquelles
    aucun coup n'est jamais joue) et y elimine, pour chacune, autant de
    joueurs au hasard que sur la table principale, en repartissant
    aleatoirement leur tapis entre les autres joueurs restants de leur
    propre table.

    Ne fait rien si la table active ne fait pas partie d'un tournoi
    multi-table (aucun tournament_id) ou si aucune elimination n'a eu lieu."""
    if n_eliminations <= 0:
        return
    idx = _load_index()
    active_gid = idx.get("active")
    my_entry = next((gm for gm in idx["games"] if gm["id"] == active_gid), None)
    if not my_entry:
        return
    tid = my_entry.get("tournament_id")
    if not tid:
        return  # partie autonome, pas de tournoi multi-table -> rien a synchroniser

    for gm in idx["games"]:
        if gm["id"] == active_gid or gm.get("tournament_id") != tid or gm.get("table_closed"):
            continue
        g = _get_game_instance(gm["id"])
        if not g.players or any(p.get("is_human") for p in g.players):
            continue  # ne synchronise que les tables 100% IA (les tables annexes)
        for _ in range(n_eliminations):
            if g.tournament_over:
                break
            victim = g.eliminate_random_player()
            if victim:
                print(f"[Table annexe : {gm['name']}] {victim} est elimine "
                      f"(elimination miroir de la table principale) et son "
                      f"tapis est reparti aleatoirement entre les autres "
                      f"joueurs de sa table.")


def _sync_satellite_pot_movement(pot_amount):
    """A appeler juste apres qu'une main vient de se terminer sur la table
    active (celle ou vous jouez, avec un siege humain), avec pot_amount = le
    pot final de cette main (game.pot au moment du showdown - ce champ n'est
    remis a zero qu'au lancement de la main suivante, voir new_hand()).

    Retrouve toutes les tables ANNEXES du meme tournoi multi-table (memes
    tournament_id, uniquement des sieges IA) et, sur chacune, fait circuler
    aleatoirement un montant equivalent entre leurs joueurs (voir
    Game.apply_pot_shuffle) : une partie de leurs jetons est prelevee au
    hasard puis integralement redistribuee au hasard, exactement comme le
    ferait une vraie main jouee sur cette table.

    Sans ce mecanisme, les tapis des tables annexes ne pourraient QU'
    augmenter (uniquement lors d'une elimination miroir, voir
    _sync_satellite_eliminations), ce qui ne reflete pas un vrai tournoi ou
    les tapis fluctuent - a la hausse comme a la baisse - a chaque main.
    Comme une vraie main, ce mouvement de pot peut au passage eliminer un ou
    plusieurs joueurs d'une table annexe (voir apply_pot_shuffle) ; ces
    eliminations sont enregistrees normalement et seront prises en compte
    par un appel a _balance_satellite_tables().

    Ne fait rien si la table active ne fait pas partie d'un tournoi
    multi-table (aucun tournament_id) ou si le montant est nul/negatif."""
    if pot_amount <= 0:
        return
    idx = _load_index()
    active_gid = idx.get("active")
    my_entry = next((gm for gm in idx["games"] if gm["id"] == active_gid), None)
    if not my_entry:
        return
    tid = my_entry.get("tournament_id")
    if not tid:
        return  # partie autonome, pas de tournoi multi-table -> rien a synchroniser

    for gm in idx["games"]:
        if gm["id"] == active_gid or gm.get("tournament_id") != tid or gm.get("table_closed"):
            continue
        g = _get_game_instance(gm["id"])
        if not g.players or any(p.get("is_human") for p in g.players):
            continue  # ne synchronise que les tables 100% IA (les tables annexes)
        eliminated = g.apply_pot_shuffle(pot_amount)
        for victim in eliminated:
            print(f"[Table annexe : {gm['name']}] {victim} est elimine "
                  f"(mouvement de pot miroir de la table principale) et son "
                  f"tapis est reparti aleatoirement entre les autres "
                  f"joueurs de sa table.")


def _balance_satellite_tables():
    """A appeler juste apres _sync_satellite_eliminations : verifie si le
    nombre de joueurs encore actifs dans le tournoi multi-table permet de
    tenir sur MOINS de tables qu'actuellement ouvertes. Si oui, ferme la ou
    les table(s) ANNEXE(S) (jamais la table principale, qui porte le siege
    humain) les moins peuplees, une par une, et repartit au hasard leurs
    joueurs restants sur les tables encore ouvertes - sans jamais depasser,
    sur aucune table, le nombre de joueurs present a cette table au debut
    du tournoi (starting_num_players).

    Ne fait rien si la table active ne fait pas partie d'un tournoi
    multi-table, ou si aucune consolidation n'est encore possible."""
    idx = _load_index()
    active_gid = idx.get("active")
    my_entry = next((gm for gm in idx["games"] if gm["id"] == active_gid), None)
    if not my_entry:
        return False
    tid = my_entry.get("tournament_id")
    if not tid:
        return False

    table_entries = [gm for gm in idx["games"] if gm.get("tournament_id") == tid]

    # Tables encore "ouvertes" : appartiennent au tournoi et n'ont pas deja
    # ete fermees lors d'un equilibrage precedent. NB : le fait qu'une table
    # annexe soit tombee toute seule a 1 joueur actif (g.tournament_over cote
    # moteur, qui n'est qu'un signal local "plus rien a eliminer ici") ne
    # doit surtout pas l'exclure : ce dernier joueur doit rester eligible a
    # une fusion avec une autre table des que possible, sinon il resterait
    # bloque indefiniment, isole du reste du tournoi.
    open_tables = []
    cap = None
    for gm in table_entries:
        if gm.get("table_closed"):
            continue
        g = _get_game_instance(gm["id"])
        if cap is None:
            cap = g.starting_num_players
        open_tables.append((gm, g))

    if cap is None or len(open_tables) <= 1:
        return False  # rien a equilibrer (tournoi solo, ou deja sur la table finale)

    def active_count(g):
        return len(g.active_players())

    total_active = sum(active_count(g) for _, g in open_tables)
    changed = False

    # Tant que le total de joueurs encore en lice tient sur un nombre de
    # tables strictement inferieur au nombre de tables ouvertes, on ferme
    # la table annexe la moins peuplee et on disperse ses joueurs.
    while len(open_tables) > 1 and total_active <= (len(open_tables) - 1) * cap:
        closable = [pair for pair in open_tables
                    if not any(p.get("is_human") for p in pair[1].players)]
        if not closable:
            break  # (cas theorique) plus aucune table annexe a fermer

        min_count = min(active_count(g) for _, g in closable)
        smallest = [pair for pair in closable if active_count(pair[1]) == min_count]
        gm_close, g_close = random.choice(smallest)

        remaining_tables = [pair for pair in open_tables if pair[0]["id"] != gm_close["id"]]

        movers = [p for p in g_close.players if p["stack"] > 0]
        random.shuffle(movers)

        for p in movers:
            capacity_left = [pair for pair in remaining_tables if active_count(pair[1]) < cap]
            if not capacity_left:
                break  # ne devrait pas arriver : garanti par la condition du while
            min_cap = min(active_count(g2) for _, g2 in capacity_left)
            choices = [pair for pair in capacity_left if active_count(pair[1]) == min_cap]
            gm_dest, g_dest = random.choice(choices)

            new_name = p["name"]
            existing_names = {pl["name"] for pl in g_dest.players}
            if new_name in existing_names:
                suffix = 2
                while f"{new_name} ({suffix})" in existing_names:
                    suffix += 1
                new_name = f"{new_name} ({suffix})"

            g_dest.players.append({
                "name": new_name, "controller": p["controller"],
                "is_human": False, "stack": p["stack"],
                # Marque ce joueur comme "fraichement arrive" sur sa nouvelle
                # table : sert a avertir son IA, dans le bloc de la PROCHAINE
                # main distribuee sur cette table, qu'elle doit desormais
                # aussi jouer ce role (voir new_hand() dans poker_engine.py,
                # qui consomme puis efface ce marqueur). Sans effet sur les
                # tables annexes, qui ne distribuent jamais de main pour de
                # vrai (elles ne font que miroiter les eliminations) - seule
                # la table principale (avec le siege humain) le consommera.
                "just_arrived": True,
                "arrival_reason": "satellite_merge",
            })
            g_dest.save()
            # Le joueur a change de table : on annule son tapis sur l'ancienne
            # table (fermee) pour qu'il n'y soit plus jamais compte comme actif
            # (il ne s'agit pas d'une elimination, donc pas d'ajout a
            # g_close.eliminations - juste un depart de table).
            p["stack"] = 0
            print(f"[Equilibrage des tables] {p['name']} quitte « {gm_close['name']} » "
                  f"(fermee) pour « {gm_dest['name']} »"
                  + (f" (renomme {new_name} pour eviter un doublon)." if new_name != p["name"] else "."))

        g_close.save()
        print(f"[Equilibrage des tables] « {gm_close['name']} » est fermee, "
              f"ses joueurs restants ont ete repartis sur les autres tables du tournoi.")

        idx2 = _load_index()
        for gm2 in idx2["games"]:
            if gm2["id"] == gm_close["id"]:
                gm2["table_closed"] = True
        _save_index(idx2)

        open_tables = remaining_tables
        changed = True

    return changed


def delete_game(gid):
    """Supprime la partie gid (et ses eventuelles tables annexes de tournoi).
    Retourne True si, apres suppression, il ne reste plus aucune partie
    dans le registre (utilise par delete_game_route pour rediriger vers la
    creation d'une nouvelle partie plutot que vers une partie vide
    auto-creee - voir get_active_game qui s'en charge de toute facon au
    prochain chargement de page si besoin)."""
    idx = _load_index()
    entry = next((gm for gm in idx["games"] if gm["id"] == gid), None)
    # Si cette partie est la table PRINCIPALE d'un tournoi multi-table (son
    # tournament_id pointe vers elle-meme, voir _is_satellite_table), il
    # faut aussi supprimer toutes ses tables ANNEXES : sinon elles restent
    # seules dans le registre, invisibles dans les onglets, et l'une
    # d'elles peut se retrouver "active" a la place d'une vraie partie.
    ids_to_remove = {gid}
    if entry and entry.get("tournament_id") == gid:
        ids_to_remove |= {
            gm["id"] for gm in idx["games"] if gm.get("tournament_id") == gid
        }

    idx["games"] = [gm for gm in idx["games"] if gm["id"] not in ids_to_remove]
    if idx.get("active") in ids_to_remove:
        # La nouvelle partie active ne doit jamais etre une table annexe
        # (elles ne sont pas navigables/jouables directement).
        remaining = [gm for gm in idx["games"] if not _is_satellite_table(gm)]
        idx["active"] = remaining[0]["id"] if remaining else None
    _save_index(idx)

    for rid in ids_to_remove:
        _GAME_CACHE.pop(rid, None)
        try:
            os.remove(_game_file(rid))
        except OSError:
            pass

    # NB : on ne recree plus automatiquement une "Partie 1" vide ici.
    # get_active_game() s'en charge de toute maniere si besoin (registre
    # vide - ou ne contenant plus que des tables annexes/orphelines - au
    # chargement d'une page) ; on renvoie ici si aucune VRAIE partie
    # (non-annexe) ne subsiste, et pas seulement si le registre est
    # litteralement vide : une table annexe orpheline qui trainerait
    # encore (ancienne sauvegarde, cas limite non couvert par
    # ids_to_remove ci-dessus) ne doit jamais etre confondue avec une
    # vraie partie restante. Cela permet a delete_game_route() de
    # rediriger vers l'ecran de creation d'une nouvelle partie (/games/new)
    # plutot que de faire atterrir l'utilisateur sur une partie fantome.
    return not any(not _is_satellite_table(gm) for gm in idx["games"])


game = None  # sera assigne automatiquement avant chaque requete (voir plus bas)
LAST_LOG = ""

# ----------------------------------------------------------------------
# Statut du relais par IA, pour le voyant affiche a cote de chaque siege
# IA sur la table : "waiting" (message envoye, reponse pas encore
# recue - rouge), "ok" (derniere reponse recue avec succes - vert),
# "error" (dernier envoi/reponse en echec - orange), ou absent du dict
# (jamais sollicitee depuis le demarrage de l'app - gris). Reinitialise
# a chaque redemarrage de l'app (etat en memoire seulement, pas persiste
# sur disque : ce n'est qu'une indication visuelle du moment present).
# ----------------------------------------------------------------------
RELAY_STATUS = {}
_relay_status_lock = threading.Lock()


def _set_relay_status(ai_type, status):
    with _relay_status_lock:
        RELAY_STATUS[ai_type] = status


def relay_status_dot_html(controller):
    """Petit voyant colore (rouge/vert/orange/gris) refletant l'etat le
    plus recent des echanges avec cette IA via le relais. Purement
    visuel : n'affecte jamais le deroulement du jeu."""
    status = RELAY_STATUS.get(controller)
    if status == "waiting":
        color, title = "#e05252", "En attente de la reponse de l'IA..."
    elif status == "ok":
        color, title = "#4caf50", "Derniere reponse recue avec succes."
    elif status == "error":
        color, title = "#e0a030", "Dernier envoi/reponse en echec - voir le journal."
    else:
        color, title = "#8a8a8a", "Pas encore sollicitee via le relais."
    safe_title = html.escape(title)
    return (
        f'<span class="relay-dot" title="{safe_title}" '
        f'style="display:inline-block;width:9px;height:9px;border-radius:50%;'
        f'background:{color};margin-left:5px;vertical-align:middle;"></span>'
    )


# ----------------------------------------------------------------------
# Pilotage automatique des IA en arriere-plan : des qu'une action fait
# passer la main a un siege IA, on lance tout seul (sans clic) la
# sequence d'echanges avec le relais, dans un thread separe pour ne
# jamais bloquer l'affichage. table_view() se recharge automatiquement
# pendant ce temps (voir auto_refresh_seconds plus bas) pour montrer la
# progression (voyants rouge -> vert).
# ----------------------------------------------------------------------
_autoplay_lock = threading.Lock()
_autoplay_running = False


def _start_autoplay_worker():
    """Marque un autoplay comme demarre et lance le thread qui l'execute.
    A n'appeler qu'apres avoir verifie (sous _autoplay_lock) qu'aucun
    autre autoplay n'est deja en cours."""
    global _autoplay_running
    _autoplay_running = True

    def _worker():
        global _autoplay_running
        try:
            _run_ai_autoplay_loop()
        finally:
            with _autoplay_lock:
                _autoplay_running = False

    threading.Thread(target=_worker, daemon=True).start()


def _maybe_start_ai_autoplay():
    """Demarre automatiquement le tour de la prochaine IA a agir, si
    le relais est configure et qu'aucun autoplay n'est deja en cours.
    A appeler apres toute action susceptible de faire passer la main a
    une IA, et au chargement de la table (pour rattraper les cas ou
    c'est deja au tour d'une IA a l'arrivee sur la page)."""
    with _autoplay_lock:
        if _autoplay_running:
            return
        if game.hand_complete or not game.to_act:
            return
        name = game.to_act[0]
        player = next((p for p in game.players if p["name"] == name), None)
        if player is None or player.get("is_human"):
            return
        cfg = load_relay_config()
        if not (cfg.get("relay_url") or "").strip():
            return
        _start_autoplay_worker()


@app.before_request
def _load_active_game():
    global game
    game = get_active_game()


@app.after_request
def no_cache(response):
    """Empeche le navigateur de mettre les pages en cache, pour toujours
    afficher l'etat reellement sauvegarde plutot qu'une version perimee."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response


def render_card(card, big=False):
    """Rendu HTML d'une carte reelle, en rectangle style carte a jouer,
    couleur rouge pour coeur/carreau, noire pour pique/trefle."""
    rank, suit = card
    symbol = SUITS[suit]
    rank_disp = RANK_NAMES[rank]
    color = "card-red" if suit in ("H", "D") else "card-black"
    size = " card-big" if big else ""
    return f'<div class="card-box {color}{size}"><span class="rank">{rank_disp}</span><span class="suit">{symbol}</span></div>'


def render_placeholder(big=False):
    size = " card-big" if big else ""
    return f'<div class="card-box placeholder{size}">?</div>'


def render_board(board, big=False):
    """Affiche les 5 emplacements du board : cartes revelees + emplacements
    en attente (avec un '?') pour les cartes pas encore sorties."""
    cells = [render_card(c, big=big) for c in board]
    cells += [render_placeholder(big=big) for _ in range(5 - len(board))]
    return '<div class="board-row">' + "".join(cells) + "</div>"


def last_action_line(action_log):
    """Renvoie la derniere ligne du journal qui decrit une VRAIE action de
    joueur (fold/check/call/raise/all-in, ou une ligne d'abattage), en
    ignorant les lignes d'en-tete (debut de main, changement de rue,
    liste des joueurs encore en jeu). None si le journal est vide ou ne
    contient encore aucune action de ce type (tout debut de main)."""
    skip_prefixes = ("Main #", "---", "Joueurs encore en jeu", ">>>", "(Note")
    for line in reversed(action_log):
        if line.startswith(skip_prefixes):
            continue
        if ":" in line:
            return line
    return None


def render_position_icon(pos):
    """Icone visuelle pour le bouton (rond avec 'B') et les blinds
    (jetons de poker : un jeton pour la SB, deux empiles pour la BB)."""
    html = ""
    if "BTN" in pos:
        html += '<span class="btn-token" title="Bouton">B</span>'
    if pos == "BB":
        html += ('<span class="chip-stack" title="Grosse blind">'
                  '<span class="chip-token"></span><span class="chip-token"></span></span>')
    elif "SB" in pos:
        html += '<span class="chip-token" title="Petite blind"></span>'
    return html


# ----------------------------------------------------------------------
# Jetons de poker (tapis, mises en cours, pot) : chaque montant est
# materialise en commençant TOUJOURS par les jetons de plus petite valeur
# (pour afficher le maximum de jetons), par PALIERS de 10 jetons. La
# petite blinde (SB) vaut UN jeton (cf `chip_unit_value()`). Des qu'un
# palier atteint 10 jetons, on passe a une couleur differente dont la
# valeur du jeton est plus elevee : le palier n (1er = blanc, 2e = rouge,
# 3e = vert, ...) vaut n fois la SB. Exemple avec SB = 100 :
#   - une mise de 600  -> 6 jetons blancs (6 x 100)
#   - une mise de 1100 -> 10 jetons blancs (1000) + 1 jeton rouge (valeur
#     200, arrondi au plus proche : 1100 est plus proche de 1000+200=1200
#     que de 1000) ; 1200 donne exactement la meme chose, 1400 donne un
#     deuxieme jeton rouge (1000+400=1400)
#   - une mise de 3200 -> 10 blancs (1000) + 10 rouges (2000) + 1 jeton
#     d'une 3e couleur valant 300 (arrondi de 200 vers 300, 3200-2000=1200,
#     soit 4 jetons de 300 = 1200 pile)
# Seul le DERNIER palier utilise (celui qui n'est pas rempli a 10 jetons)
# est arrondi ; tous les paliers precedents sont toujours pleins (10
# jetons), ce qui garantit d'afficher le plus de jetons possible tout en
# restant lisible. Aucun chiffre n'est ecrit sur les jetons eux-memes ; le
# montant exact reste visible en infobulle (survol).
# ----------------------------------------------------------------------

# Couleurs successives des paliers (palier 1 = SB = 1x l'unite, palier 2 =
# 2x, palier 3 = 3x, etc.), palette inspiree des vrais jetons de casino.
# Si un montant necessite plus de paliers que de couleurs listees, la
# palette est reutilisee en boucle (cas extreme, tres improbable en jeu).
TIER_COLORS = [
    "#f5f0e6",  # 1 - blanc
    "#c8203a",  # 2 - rouge
    "#1a8f4c",  # 3 - vert
    "#2b2b2b",  # 4 - noir
    "#8b3fd1",  # 5 - violet
    "#f2c40c",  # 6 - or
    "#1f5fd1",  # 7 - bleu
    "#b7752e",  # 8 - bronze
]

MAX_CHIPS_PER_TIER = 10  # nombre de jetons d'une couleur avant de changer de palier

# Nombre maximum de "piles" (paliers/couleurs differentes) que l'on
# souhaite jamais afficher simultanement pour un seul montant (tapis,
# mise ou pot), quelle que soit la taille de LA TABLE affichee (nombre
# de joueurs x tapis de depart SUR CETTE TABLE - voir chip_tier_step(),
# qui calcule ce total a partir du `game` passe en parametre, donc de la
# seule table en cours de rendu, meme dans un tournoi multi-table). Voir
# `chip_tier_step()` ci-dessous : c'est cette valeur qui sert a calculer
# de combien la valeur d'un jeton doit sauter d'une couleur a l'autre
# pour ne jamais depasser ce nombre de piles, meme sur une table a 10
# joueurs avec 60 000 de tapis de depart.
MAX_TOWER_COLORS = 9


def chip_unit_value(game):
    """Valeur (en jetons) d'UN seul jeton du 1er palier (blanc) : la
    petite blinde (SB) en cours, qui vaut par definition exactement 1
    jeton (la grosse blinde, le double de la SB, en vaut donc 2 - c'est
    le point de depart naturel du poker). Repli sur la grosse blinde
    (divisee par 2) puis sur 1 si la petite blinde n'est pas definie."""
    try:
        sb = float(getattr(game, "small_blind", None))
        if sb > 0:
            return sb
    except (TypeError, ValueError):
        pass
    try:
        bb = float(getattr(game, "big_blind", None))
        if bb > 0:
            return bb / 2
    except (TypeError, ValueError):
        pass
    return 1.0


def chip_tier_step(game, unit_value=None, max_towers=MAX_TOWER_COLORS,
                    max_per_tier=MAX_CHIPS_PER_TIER):
    """Calcule le multiplicateur a appliquer entre 2 paliers consecutifs
    de `chip_breakdown()` (l'"ecart de valeur" entre 2 couleurs de
    jetons) afin qu'aucun montant ne necessite jamais plus de
    `max_towers` paliers/couleurs differentes, MEME pour le plus gros
    montant jamais possible SUR CETTE TABLE (par definition, la totalite
    des jetons presents SUR CETTE TABLE : `starting_stack x
    starting_num_players` DE CETTE TABLE, une quantite CONSTANTE puisque
    les jetons ne font que circuler entre joueurs/pot de la meme table -
    cf commentaire dans PokerGame.__init__). Dans un tournoi multi-table,
    `game` designe toujours une seule table (celle en cours de rendu) :
    ce total n'additionne donc jamais les jetons des autres tables du
    tournoi, seulement ceux de la table affichee.

    Avec un multiplicateur `step`, le palier n vaut n x unit_value x
    step (au lieu de n x unit_value) : plus `step` est grand, plus la
    valeur des jetons augmente vite d'une couleur a l'autre, et moins
    il faut de paliers pour representer un gros montant. On choisit
    le plus petit `step` (jamais < 1, pour ne rien changer aux petites
    tables ou `step` ne serait pas necessaire) qui garantit que
    `max_towers` paliers PLEINS suffisent a couvrir le total de jetons
    de la table :

        max_per_tier * unit_value * step * max_towers*(max_towers+1)/2
            >= total_jetons_de_la_table

    Resultat : quelle que soit la taille de la table (nombre de joueurs
    x tapis de depart SUR CETTE TABLE), un tapis, une mise ou le pot ne
    s'affichera jamais avec plus de `max_towers` couleurs de jetons
    differentes. Comme ce calcul se base sur `game` (donc sur LA MEME
    table pour tous les joueurs assis a cette table) et est utilise pour
    materialiser tapis, mises et pot de cette meme table, la valeur d'un
    jeton reste forcement identique pour tout le monde a cette table des
    qu'elle change."""
    if unit_value is None:
        unit_value = chip_unit_value(game)
    try:
        unit_value = float(unit_value)
    except (TypeError, ValueError):
        unit_value = 1.0
    if unit_value <= 0:
        unit_value = 1.0

    try:
        total = float(getattr(game, "starting_stack", 0) or 0) * \
            float(getattr(game, "starting_num_players", 0) or 0)
    except (TypeError, ValueError):
        total = 0.0

    if total <= 0 or max_towers <= 0 or max_per_tier <= 0:
        return 1.0

    capacity_at_step_1 = max_per_tier * unit_value * max_towers * (max_towers + 1) / 2
    if capacity_at_step_1 <= 0:
        return 1.0

    return max(1.0, total / capacity_at_step_1)


def _round_half_up(x):
    """Arrondi 'humain' (0,5 arrondit toujours vers le haut), plus
    intuitif ici que l'arrondi bancaire par defaut de Python (qui
    arrondirait 0,5 vers le bas dans la moitie des cas)."""
    return math.floor(x + 0.5)


def chip_breakdown(amount, unit_value, max_per_tier=MAX_CHIPS_PER_TIER, max_tower_height=10,
                    tier_step=1.0):
    """Decompose `amount` en jetons par PALIERS CROISSANTS (voir le
    commentaire de module ci-dessus) : le palier n vaut n x `unit_value`
    x `tier_step` et peut contenir au maximum `max_per_tier` jetons. On
    remplit toujours le palier le plus bas en premier ; des qu'il est
    plein (max_per_tier atteint) ET qu'il reste un montant a representer,
    on passe au palier suivant (valeur plus elevee, couleur differente).
    Le DERNIER palier utilise (celui qui n'atteint pas max_per_tier) est
    arrondi au jeton le plus proche (minimum 1 si un reste existe).

    `tier_step` (par defaut 1, comme avant) permet d'ecarter davantage
    la valeur des jetons d'un palier a l'autre : utile pour les gros
    tournois, ou sans cela le nombre de paliers necessaires pour
    representer un gros tapis exploserait. Voir `chip_tier_step()`, qui
    calcule ce multiplicateur a partir du total de jetons du tournoi
    pour ne jamais depasser un nombre de piles donne. `unit_value` ET
    `tier_step` doivent etre calcules une seule fois par table (a
    partir du meme `game`) et reutilises pour le tapis de chaque
    joueur, chaque mise et le pot : c'est ce qui garantit que la valeur
    d'un jeton reste identique pour tout le monde a tout instant.

    Renvoie une liste de tours (color, hauteur) pretes pour l'affichage :
    chaque palier peut occuper plusieurs tours de la meme couleur si son
    nombre de jetons depasse `max_tower_height` (purement visuel, pour
    eviter des piles trop hautes a l'ecran). `max_tower_height` vaut par
    defaut `max_per_tier` (10) : un palier plein tient donc dans UNE
    seule tour de 10 jetons empiles, ce qui limite le nombre de tours
    affichees cote a cote."""
    try:
        amount = float(amount)
        unit_value = float(unit_value)
        tier_step = float(tier_step)
    except (TypeError, ValueError):
        return []
    if amount <= 0 or unit_value <= 0:
        return []
    if tier_step <= 0:
        tier_step = 1.0

    counts = []  # [(color, count)] dans l'ordre des paliers (bas -> haut)
    remaining = amount
    tier = 1
    max_tiers = 200  # garde-fou (ne devrait jamais etre atteint en jeu normal)
    while remaining > 0 and tier <= max_tiers:
        denom = tier * unit_value * tier_step
        color = TIER_COLORS[(tier - 1) % len(TIER_COLORS)]
        exact_count = remaining / denom
        if exact_count >= max_per_tier:
            # palier plein : toujours max_per_tier jetons pile, on passe
            # au palier suivant avec ce qui reste.
            counts.append((color, max_per_tier))
            remaining -= max_per_tier * denom
            tier += 1
        else:
            # dernier palier necessaire : on arrondit au jeton le plus
            # proche (au moins 1 jeton tant qu'il reste un montant a
            # representer).
            count = max(1, _round_half_up(exact_count))
            counts.append((color, count))
            remaining = 0

    towers = []
    for color, count in counts:
        remaining_count = count
        while remaining_count > 0:
            h = min(max_tower_height, remaining_count)
            towers.append((color, h))
            remaining_count -= h
    return towers


FRONT_ROW_TOWERS = 3  # nombre de colonnes par "ligne" avant de passer a une ligne suivante


def render_chip_cluster(towers, size="sm", title="", row_towers=FRONT_ROW_TOWERS):
    """Rendu HTML d'une grappe de tours de jetons (une couleur par
    palier, cf chip_breakdown()). Purement visuel : aucun montant
    chiffre n'est affiche ; un `title` optionnel porte le montant exact,
    visible en infobulle au survol, sans encombrer l'affichage.

    Pour eviter qu'une grappe avec beaucoup de paliers/tours ne devienne
    trop large, les tours sont regroupees par LIGNES de `row_towers`
    colonnes (3 par defaut ; le pot, plus large, en utilise davantage
    pour limiter le nombre de lignes empilees - voir plus bas). La 1ere
    ligne (les `row_towers` premieres tours) s'affiche "en ligne",
    normalement, et sert de reference (base) pour les suivantes.
    A partir de la 2eme ligne, chaque tour est posee EN SUPERPOSITION
    avec la tour de la MEME COLONNE dans la ligne precedente, mais
    DEVANT elle (z-index superieur, donc elle la recouvre) et
    legerement PLUS BAS (sa base descend d'un cran a chaque nouvelle
    ligne) :
    - comme elle est devant et plus basse, elle cache progressivement le
      bas de la ligne precedente a mesure qu'elle grandit ;
    - si les 2 tours atteignent leur hauteur max, la ligne precedente ne
      depasse alors plus que par le HAUT, sur une hauteur d'un seul
      jeton (le jeton du haut de la ligne precedente reste visible,
      jamais plus).
    Cote horizontal, chaque ligne se decale par rapport a la precedente,
    mais en alternant le sens, INCONDITIONNELLEMENT (quel que soit le
    nombre de jetons de la tour, y compris un reste de 1-3 jetons) : la
    ligne 2 se decale a DROITE par rapport a la ligne 1, la ligne 3 a
    GAUCHE par rapport a la ligne 2, la ligne 4 a DROITE par rapport a
    la ligne 3, etc. (zigzag). Le decalage etant cumulatif (chaque
    ligne se decale par rapport a la position REELLE de la precedente,
    pas par rapport a la colonne de base), une seule ligne qui ne se
    decalerait pas desynchroniserait tout le zigzag des lignes
    suivantes de sa colonne - d'ou l'absence de tout seuil ici.
    Ce meme principe s'applique recursivement, ligne apres ligne, sans
    jamais elargir la grappe au-dela de la largeur de la 1ere ligne (le
    zigzag horizontal reste borne par `side_peek`).

    Comme chaque ligne supplementaire ajoute de la profondeur (devant +
    plus bas) et donc du desordre visuel au-dela de 2-3 lignes, on
    privilegie toujours des tours moins nombreuses mais plus hautes
    (via `max_tower_height` dans chip_breakdown()) et, pour les grappes
    larges comme le pot, davantage de colonnes par ligne (`row_towers`)
    plutot que de laisser s'empiler trop de lignes."""
    if not towers:
        return ""
    size_cls = " pkchip-lg" if size == "lg" else ""
    is_lg = size == "lg"
    # Hauteur du 1er jeton pose ; chaque jeton suivant ne rajoute que
    # `overlap_step` du fait du chevauchement defini dans le CSS (cf
    # .pkchip-disc, `margin-top` negatif == `step` ci-dessous).
    disc_h = 8 if is_lg else 6
    step = 4.6 if is_lg else 3.4
    overlap_step = disc_h - step  # gain de hauteur reel par jeton supplementaire empile
    tower_w = 14 if is_lg else 10  # largeur d'un jeton (cf .pkchip-disc / .pkchip-disc.pkchip-lg)
    col_gap = 3  # doit correspondre au `gap` de .pkchip-cluster
    side_peek = 7 if is_lg else 5  # decalage lateral (zigzag) applique UNE FOIS le seuil atteint
    row_drop = disc_h  # abaissement vertical d'une ligne par rapport a la precedente :
    # egal a la hauteur d'un jeton, de sorte que si les 2 tours font 10
    # jetons, seul le jeton du haut de la ligne precedente reste visible.

    def tower_px_height(n):
        """Hauteur (px) d'une tour de `n` jetons empiles."""
        return 0 if n <= 0 else disc_h + (n - 1) * overlap_step

    def col_x(col):
        """Position horizontale (px) de la colonne `col`, alignee sur
        l'espacement normal (gap) de la 1ere ligne."""
        return col * (tower_w + col_gap)

    towers_html = ""
    prev_row_bottoms = None  # base absolue (px) de chaque colonne de la ligne precedente
    prev_row_lefts = None  # decalage horizontal absolu (px) de chaque colonne de la ligne precedente
    min_bottom = 0.0  # plus bas point (px, <=0) atteint par une colonne, toutes lignes confondues
    row_count = (len(towers) + row_towers - 1) // row_towers
    for row in range(row_count):
        row_bottoms = [0.0] * row_towers
        row_lefts = [col_x(c) for c in range(row_towers)]
        for col in range(row_towers):
            idx = row * row_towers + col
            if idx >= len(towers):
                continue
            color, count = towers[idx]
            discs = "".join(
                f'<span class="pkchip-disc{size_cls}" style="--chip-c:{color};"></span>'
                for _ in range(count)
            )
            if row == 0:
                towers_html += f'<span class="pkchip-tower">{discs}</span>'
                row_bottoms[col] = 0.0
            else:
                # Devant la ligne precedente (z-index croissant) et
                # legerement plus bas (base decalee vers le bas de
                # `row_drop`), colonne par colonne.
                bottom = prev_row_bottoms[col] - row_drop
                min_bottom = min(min_bottom, bottom)
                # Zigzag : le sens du decalage lateral s'inverse a
                # chaque ligne (droite / gauche / droite / ...).
                # Applique inconditionnellement (jamais de seuil sur le
                # nombre de jetons) : sinon la derniere tour d'une pile
                # (souvent un reste de 1 a 3 jetons) ne se decalerait
                # pas, ce qui desynchronise en cascade le zigzag de
                # toutes les lignes suivantes de cette colonne (le
                # decalage etant cumulatif par rapport a la position
                # reelle de la ligne precedente).
                direction = 1 if row % 2 == 1 else -1
                left = prev_row_lefts[col] + direction * side_peek
                z_index = 100 + row * 10
                style = (
                    f'left:{round(left, 1)}px; bottom:{round(bottom, 1)}px; '
                    f'z-index:{z_index};'
                )
                towers_html += (
                    f'<span class="pkchip-tower pkchip-tower-over" style="{style}">{discs}</span>'
                )
                row_bottoms[col] = bottom
                row_lefts[col] = left
        prev_row_bottoms = row_bottoms
        prev_row_lefts = row_lefts
    title_attr = f' title="{title}"' if title else ""
    # Les lignes suivantes debordent sous la 1ere ligne (bases negatives) ;
    # on reserve cet espace en marge basse pour que ce qui suit la grappe
    # dans le HTML (ex. le libelle "Pot") ne soit pas recouvert.
    overhang_style = f' style="margin-bottom:{round(-min_bottom, 1)}px;"' if min_bottom < 0 else ""
    return f'<span class="pkchip-cluster"{title_attr}{overhang_style}>{towers_html}</span>'



def render_poker_table(seats, pot=None, current_bets=None, unit_value=None, tier_step=1.0):
    """Dessine une table de poker ovale avec les joueurs places tout autour,
    chacun affichant sa position, son nom et ses infos (gere par / rang
    au classement) juste en dessous. Le TAPIS de chaque joueur
    est materialise par une grappe de jetons REELS (plusieurs
    denominations/couleurs, cf chip_breakdown()) posee SUR LE BORD du
    dessin de la table, legerement decalee dans le sens des aiguilles
    d'une montre par rapport au siege (comme sur une vraie table, les
    jetons d'un joueur sont a sa droite). `seats` est une liste de dicts :
    {pos, name, controller, stack, is_human, folded, is_next, rank}.
    `current_bets` (optionnel) est le dict nom -> montant engage dans la
    rue en cours (game.current_bets) : chaque mise est materialisee par
    une grappe posee entre le siege et le centre, et le pot par une
    grappe au centre.

    `unit_value` est la valeur (en jetons) d'UN seul jeton du 1er palier,
    utilisee pour TOUTES les grappes (tapis, mises, pot) : une seule et
    meme echelle, partagee par tout le monde a un instant donne (cf
    chip_unit_value(), qui recommande la petite blinde en cours).
    Consequence voulue : un montant donne a toujours la meme taille de
    grappe, qu'il s'agisse d'un tapis ou d'une mise/pot (echelle
    commune), et l'ecart entre deux montants est directement visible.
    Si `unit_value` est absent/invalide, on retombe sur 1 (chaque jeton
    du dessin vaut alors 1 jeton reel). Aucun chiffre n'est ecrit sur les
    jetons eux-memes (voir chip_breakdown()).

    `tier_step` (par defaut 1, comme avant) est le multiplicateur
    d'ecart entre 2 couleurs de jetons (cf chip_tier_step()), calcule a
    partir du total de jetons du tournoi pour ne jamais depasser un
    nombre de piles donne. Comme `unit_value`, c'est une seule et meme
    valeur partagee par tapis/mises/pot : si elle change (gros
    tournoi), elle change identiquement pour tout le monde.

    Si un seul siege a `is_human` a True, ce joueur est fixe visuellement
    en HAUT de la table (siege le plus facile a retrouver d'un coup
    d'oeil) : ce sont alors les jetons de position (bouton, petite/grosse
    blind) qui tournent d'un siege a l'autre au fil des mains, comme sur
    une vraie table ou un joueur ne change pas de place mais le bouton
    passe de main en main. Avec zero ou plusieurs joueurs humains,
    l'ordre des sieges recu (BTN en premier) est conserve tel quel."""
    current_bets = current_bets or {}
    seats = list(seats)  # rotation locale : ne modifie pas la liste de l'appelant

    human_idxs = [i for i, s in enumerate(seats) if s.get("is_human")]
    if len(human_idxs) == 1:
        offset = human_idxs[0]
        seats = seats[offset:] + seats[:offset]

    n = max(len(seats), 1)
    # rayons (en %) de l'ellipse sur laquelle sont places les sieges : proches
    # du bord du cadre (le feutre visuel est volontairement plus petit, voir
    # felt_rx/felt_ry ci-dessous et .poker-table-felt) pour degager un espace
    # entre le bloc siege et la table et eviter le chevauchement avec les jetons.
    rx, ry = 49, 46
    # rayon (en %) du feutre vert dessine (plus petit que le cadre complet) :
    # les jetons de tapis se posent sur son bord, nettement en retrait des sieges.
    felt_rx, felt_ry = 37, 33
    stack_rx, stack_ry = felt_rx * 0.95, felt_ry * 0.95  # jetons du tapis : sur le bord du feutre
    bet_rx, bet_ry = felt_rx * 0.55, felt_ry * 0.55  # rayon reduit : mises entre siege et centre
    # decalage angulaire (sens horaire) entre un siege et la grappe de
    # jetons de son tapis : une fraction de l'ecart entre deux sieges,
    # pour rester "a cote" du siege sans chevaucher le voisin.
    stack_angle_offset = math.radians(min(30, (360 / n) * 0.4))
    seats_html = ""
    bets_html = ""
    stacks_html = ""

    pot_value = pot if isinstance(pot, (int, float)) else 0
    unit = unit_value if isinstance(unit_value, (int, float)) and unit_value > 0 else 1
    step = tier_step if isinstance(tier_step, (int, float)) and tier_step > 0 else 1.0

    for i, s in enumerate(seats):
        angle = math.radians(-90 + i * (360 / n))
        x = 50 + rx * math.cos(angle)
        y = 50 + ry * math.sin(angle)
        classes = "poker-seat"
        if s.get("is_next"):
            classes += " seat-active"
        if s.get("is_human"):
            classes += " seat-me"
        if s.get("folded"):
            classes += " seat-folded"
        pos_icon = render_position_icon(s["pos"]) if s.get("pos") and s["pos"] != "-" else ""
        name_tag = " (vous)" if s.get("is_human") else ""
        folded_tag = ' <span class="tag">couche</span>' if s.get("folded") else ""
        allin_tag = ' <span class="tag tag-allin">all-in</span>' if s.get("all_in") and not s.get("folded") else ""
        rank = s.get("rank", "-")
        rank_cls = " rank-lead" if rank == 1 else ""
        controller_dot = "" if s.get("is_human") else relay_status_dot_html(s.get("controller", ""))
        seats_html += f"""
        <div class="{classes}" style="left:{x:.2f}%; top:{y:.2f}%;">
          <div class="seat-box">
            <div class="seat-pos-row"><span>{s.get('pos', '-')}</span>{pos_icon}</div>
            <div class="seat-name">{s['name']}{name_tag}{folded_tag}{allin_tag}</div>
            <div class="seat-info">
              {s.get('controller', '-')}{controller_dot}
              <span class="{rank_cls.strip()}">#{rank}</span><br>
              Tapis {s.get('stack', '-')}
            </div>
          </div>
        </div>
        """
        # Grappe de jetons du tapis : posee sur le bord de la table, a
        # cote du siege dans le sens horaire (pas dans la case du siege).
        # Purement visuelle, sans libelle ni montant (deja affiches dans
        # la bulle d'information du siege ci-dessus).
        stack_amount = s.get("stack")
        if isinstance(stack_amount, (int, float)) and stack_amount > 0 and not s.get("folded"):
            stack_angle = angle + stack_angle_offset
            stx = 50 + stack_rx * math.cos(stack_angle)
            sty = 50 + stack_ry * math.sin(stack_angle)
            towers = chip_breakdown(stack_amount, unit, tier_step=step)
            stack_html = render_chip_cluster(towers, title=f"Tapis : {stack_amount}")
            if stack_html:
                stacks_html += f"""
                <div class="table-stack-chip" style="left:{stx:.2f}%; top:{sty:.2f}%;">
                  {stack_html}
                </div>
                """
        bet_amount = current_bets.get(s.get("name")) if not s.get("folded") else None
        if bet_amount:
            bx = 50 + bet_rx * math.cos(angle)
            by = 50 + bet_ry * math.sin(angle)
            bet_towers = chip_breakdown(bet_amount, unit, tier_step=step)
            bets_html += f"""
            <div class="table-bet-chip" style="left:{bx:.2f}%; top:{by:.2f}%;">
              {render_chip_cluster(bet_towers, title=f"Mise : {bet_amount}")}
            </div>
            """
    center_html = ""
    if pot is not None and pot_value > 0:
        # max_tower_height plus eleve et davantage de colonnes par ligne
        # (row_towers) que la valeur par defaut : le pot peut cumuler
        # beaucoup de paliers de jetons, et comme chaque ligne
        # supplementaire ajoute de la profondeur (devant + plus bas),
        # mieux vaut moins de tours (plus hautes) et plus de colonnes
        # par ligne pour eviter d'empiler trop de lignes et de donner
        # un rendu brouillon au centre de la table.
        pot_towers = chip_breakdown(pot_value, unit, max_tower_height=10, tier_step=step)
        pot_pile_html = render_chip_cluster(
            pot_towers, title=f"Pot : {pot_value}", row_towers=5
        )
        center_html = (
            '<div class="poker-table-center">'
            f'{pot_pile_html}<span class="pot-label">Pot</span></div>'
        )
    return f"""
    <div class="poker-table-wrap">
      <div class="poker-table-oval">
        <div class="poker-table-felt"></div>
        {center_html}
        {stacks_html}
        {bets_html}
        {seats_html}
      </div>
    </div>
    """


def run_captured(fn, *args, **kwargs):
    """Execute une fonction du moteur en capturant tout ce qu'elle imprime,
    pour l'afficher ensuite dans le journal de la page web."""
    global LAST_LOG
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*args, **kwargs)
    LAST_LOG = buf.getvalue()
    return LAST_LOG


BASE_CSS = """
<style>
  :root{
    --felt:#0e3b2e; --felt-dark:#0a2b21; --cream:#f2ead9;
    --gold:#c9a24b; --gold-soft:#e8cf8a; --ink:#1a1a1a;
    --red:#b3432b; --line:rgba(242,234,217,0.12);
  }
  *{box-sizing:border-box;}
  html, body{margin:0; padding:0;}
  .bg-chip{
    position:absolute; border-radius:50%; z-index:-1;
    background:repeating-conic-gradient(var(--cream) 0deg 30deg, var(--chip-color, var(--red)) 30deg 60deg);
    border:3px solid rgba(0,0,0,0.35);
    box-shadow:0 6px 14px rgba(0,0,0,0.45);
    opacity:0.16;
  }
  body{
    color:var(--cream); font-family:-apple-system,Segoe UI,Roboto,sans-serif;
    padding:16px; padding-bottom:60px; position:relative;
    background-color:var(--felt-dark);
    background-image:
      repeating-linear-gradient(45deg, rgba(255,255,255,0.012) 0 2px, transparent 2px 6px),
      radial-gradient(circle at 50% -10%, var(--felt) 0%, var(--felt-dark) 70%);
  }
  h1{
    font-family:Georgia,'Times New Roman',serif; letter-spacing:0.5px;
    font-size:1.5rem; margin:0 0 4px 0; color:var(--gold-soft);
  }
  h2{font-size:1rem; color:var(--gold-soft); margin:22px 0 8px 0; border-bottom:1px solid var(--line); padding-bottom:6px;}
  .sub{color:#b9c9bd; font-size:0.85rem; margin-bottom:18px;}
  .card{
    background:rgba(242,234,217,0.05); border:1px solid var(--line);
    border-radius:10px; padding:14px; margin-bottom:14px;
  }
  table{width:100%; border-collapse:collapse; font-size:0.9rem;}
  td,th{padding:6px 4px; text-align:left; border-bottom:1px solid var(--line);}
  .me{color:var(--gold-soft); font-weight:bold;}
  .pos{display:inline-block; min-width:38px; font-weight:bold; color:var(--gold);}
  .pos-cell{white-space:nowrap;}
  input,select,textarea{
    width:100%; padding:9px; margin:5px 0 12px 0; border-radius:6px;
    border:1px solid var(--line); background:rgba(0,0,0,0.25); color:var(--cream);
    font-size:0.95rem;
  }
  label{font-size:0.8rem; color:#b9c9bd;}
  button, .btn{
    background:var(--gold); color:var(--ink); border:none; border-radius:8px;
    padding:11px 16px; font-size:0.95rem; font-weight:bold; cursor:pointer;
    margin:4px 4px 4px 0; display:inline-block; text-decoration:none;
  }
  button.secondary, .btn.secondary{background:transparent; color:var(--gold-soft); border:1px solid var(--gold);}
  button.danger{background:var(--red); color:var(--cream);}
  .board-row{
    display:grid; grid-template-columns:repeat(5, 1fr); gap:5px;
    margin:10px 0 4px 0; max-width:340px;
  }
  .my-cards-row{display:flex; gap:10px; margin:8px 0 4px 0;}
  .flip-player-block{display:inline-block; margin:6px 14px 10px 0; vertical-align:top;}
  .flip-player-name{font-weight:bold; color:var(--gold-soft); margin-bottom:4px; font-size:0.9rem;}
  .flip-outer{perspective:800px; width:138px; height:90px; cursor:pointer;}
  .flip-inner{
    position:relative; width:100%; height:100%;
    transition:transform 0.5s; transform-style:preserve-3d;
  }
  .flip-outer.revealed .flip-inner{transform:rotateY(180deg);}
  .flip-face{
    position:absolute; inset:0; backface-visibility:hidden;
    border-radius:8px; display:flex; align-items:center; justify-content:center;
  }
  .card-back-face{
    background:repeating-linear-gradient(45deg, #0e3b2e 0 8px, #0a2b21 8px 16px);
    border:2px solid var(--gold); color:var(--gold-soft); font-size:1.8rem;
    flex-direction:column; gap:2px;
  }
  .card-back-face .flip-hint{font-size:0.62rem; font-weight:bold; opacity:0.85;}
  .card-front-face{transform:rotateY(180deg); gap:6px;}
  .card-box{
    width:52px; height:74px; border-radius:8px; background:var(--cream);
    border:2px solid rgba(0,0,0,0.15); box-shadow:0 2px 4px rgba(0,0,0,0.35);
    display:flex; flex-direction:column; align-items:center; justify-content:center;
    font-weight:bold; line-height:1.1;
  }
  .card-box .rank{font-size:0.85rem;}
  .card-box .suit{font-size:1.3rem; margin-top:1px;}
  .card-box.card-big{width:64px; height:90px;}
  .card-box.card-big .rank{font-size:1rem;}
  .card-box.card-big .suit{font-size:1.6rem;}
  .board-row .card-box{
    width:100%; height:auto; aspect-ratio:0.72;
  }
  .board-row .card-box .rank{font-size:clamp(0.5rem, 3vw, 0.9rem);}
  .board-row .card-box .suit{font-size:clamp(0.85rem, 4.5vw, 1.5rem);}
  .card-box.card-red{color:#b3273b;}
  .card-box.card-black{color:#111;}
  .card-box.placeholder{
    background:rgba(242,234,217,0.08); border:2px dashed var(--line);
    color:var(--gold-soft); font-size:1.4rem; box-shadow:none;
  }
  .btn-token{
    display:inline-block; width:20px; height:20px; border-radius:50%;
    background:var(--gold); color:var(--ink); font-weight:bold; font-size:0.7rem;
    line-height:20px; text-align:center; vertical-align:middle; margin-left:5px;
    box-shadow:0 1px 3px rgba(0,0,0,0.45);
  }
  .chip-token{
    display:inline-block; width:16px; height:16px; border-radius:50%;
    background:repeating-conic-gradient(var(--cream) 0deg 30deg, var(--red) 30deg 60deg);
    border:1px solid rgba(0,0,0,0.5); vertical-align:middle; margin-left:5px;
    box-shadow:0 1px 3px rgba(0,0,0,0.45);
  }
  .chip-stack{
    display:inline-block; position:relative; width:24px; height:16px;
    vertical-align:middle; margin-left:5px;
  }
  .chip-stack .chip-token{position:absolute; top:0; margin-left:0;}
  .chip-stack .chip-token:nth-child(1){left:0;}
  .chip-stack .chip-token:nth-child(2){left:7px;}
  .row-name{display:flex; flex-direction:column; gap:2px;}
  .row-name .tag{align-self:flex-start;}
  .rank-lead{color:var(--gold-soft); font-weight:bold;}
  .preset-card{cursor:pointer; transition:border-color 0.15s, background 0.15s;}
  .preset-card:active{opacity:0.85;}
  .preset-selected{border-color:var(--gold) !important; background:rgba(201,162,75,0.12) !important;}
  .tabs-row{
    display:flex; gap:6px; flex-wrap:wrap; margin:4px 0 16px 0;
    border-bottom:1px solid var(--line); padding-bottom:8px;
  }
  .game-tab{
    background:rgba(242,234,217,0.06); color:var(--cream); border:1px solid var(--line);
    border-radius:7px 7px 0 0; padding:7px 12px; font-size:0.85rem; text-decoration:none;
    display:inline-block;
  }
  .game-tab.tab-active{background:var(--gold); color:var(--ink); font-weight:bold; border-color:var(--gold);}
  .game-tab.game-tab-new{background:transparent; border-style:dashed; color:var(--gold-soft);}
  .grid{display:grid; grid-template-columns:1fr 1fr; gap:10px;}
  pre.log{
    background:#08211a; color:#9fd6b9; padding:12px; border-radius:8px;
    font-size:0.72rem; overflow-x:auto; max-height:260px; white-space:pre-wrap;
  }
  textarea.copybox{font-family:monospace; font-size:0.8rem; min-height:180px;}
  .copy-wrap{position:relative;}
  .copy-msg{font-size:0.75rem; color:var(--gold-soft); margin-left:6px;}
  .tag{display:inline-block; background:rgba(201,162,75,0.18); color:var(--gold-soft);
       border-radius:5px; padding:1px 7px; font-size:0.72rem; margin-left:6px;}
  .tag-allin{background:rgba(200,32,58,0.28); color:#ffb4bf;}
  .last-action-banner{
    background:rgba(201,162,75,0.12); border-left:3px solid var(--gold);
    padding:6px 10px; margin:8px 0; font-size:0.85rem; color:var(--cream);
    border-radius:0 6px 6px 0;
  }
  .to-call-text{font-size:0.8rem; color:var(--gold-soft); margin:2px 0 8px 0;}
  .row{display:flex; gap:8px; flex-wrap:wrap; align-items:center;}

  /* -------- Table de poker visuelle (ovale + sieges autour) -------- */
  .poker-table-wrap{
    position:relative; width:100%; max-width:560px;
    margin:10px auto 22px auto; padding:0 4px;
  }
  .poker-table-oval{
    position:relative; width:100%; padding-top:58%;
  }
  .poker-table-felt{
    position:absolute; left:13%; right:13%; top:17%; bottom:17%;
    background:radial-gradient(ellipse at 50% 42%, #175f42 0%, #0e3b2e 55%, #0a2b21 100%);
    border:solid #6b4423; border-width:clamp(6px, 1.6vw, 10px);
    border-radius:50%;
    box-shadow:inset 0 0 46px rgba(0,0,0,0.55), 0 10px 22px rgba(0,0,0,0.5);
  }
  .poker-table-center{
    position:absolute; top:50%; left:50%; transform:translate(-50%,-50%);
    text-align:center; color:#b9c9bd; font-size:0.68rem; white-space:nowrap;
  }
  .poker-seat{
    position:absolute; transform:translate(-50%,-50%);
    width:clamp(50px, 18vw, 84px); text-align:center;
  }
  .poker-seat .seat-box{
    background:transparent; border:1px solid transparent; border-radius:9px;
    padding:1px 2px; transition:border-color 0.15s, box-shadow 0.15s;
    text-shadow:0 1px 3px rgba(0,0,0,0.9), 0 0 6px rgba(0,0,0,0.7);
  }
  .poker-seat.seat-active .seat-box{
    border-color:var(--gold); box-shadow:0 0 8px rgba(201,162,75,0.5);
  }
  .poker-seat.seat-folded .seat-box{opacity:0.42;}
  .seat-pos-row{
    font-size:0.52rem; color:var(--gold); font-weight:bold;
    display:flex; align-items:center; justify-content:center; gap:2px; white-space:nowrap;
  }
  .seat-name{
    font-weight:bold; font-size:clamp(0.52rem, 1.8vw, 0.64rem); margin:1px 0 1px 0;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
  }
  .poker-seat.seat-me .seat-name{color:var(--gold-soft);}
  .seat-info{font-size:0.5rem; color:#cfe3d6; line-height:1.2;}
  .seat-info .rank-lead{color:var(--gold-soft); font-weight:bold;}

  /* -------- Jetons de poker (tapis / mises / pot) -------- *
   * Chaque montant est materialise par PALIERS de 10 jetons (voir
   * chip_breakdown() en Python) : la SB vaut 1 jeton du 1er palier
   * (blanc), et des qu'un palier atteint 10 jetons on passe a une
   * couleur/valeur superieure. Aucun chiffre n'est ecrit sur les jetons ;
   * le montant exact n'apparait qu'en infobulle (attribut title) au
   * survol. */
  .pkchip-cluster{display:inline-flex; align-items:flex-end; gap:3px; vertical-align:middle; position:relative;}
  .pkchip-tower{display:flex; flex-direction:column-reverse; align-items:center; position:relative; z-index:100;}
  .pkchip-tower .pkchip-disc{
    --chip-c: var(--red);
    width:10px; height:5px; border-radius:50%;
    background:repeating-conic-gradient(var(--cream) 0deg 26deg, var(--chip-c) 26deg 52deg);
    border:1px solid rgba(0,0,0,0.55);
    box-shadow:0 1px 1px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.28);
    margin-top:-2.8px;
  }
  .pkchip-tower .pkchip-disc:last-child{margin-top:0;}
  .pkchip-tower .pkchip-disc.pkchip-lg{width:14px; height:6.5px; margin-top:-3.7px;}
  /* Tours au-dela de la 3e colonne (cf FRONT_ROW_TOWERS/render_chip_cluster) :
     posees DEVANT la ligne precedente (z-index superieur) et legerement
     plus bas, pour ne pas elargir la grappe tout en recouvrant
     progressivement le bas de la ligne precedente. `left`, `bottom` et
     `z-index` viennent du style inline calcule cote Python, propres a
     chaque tour. */
  .pkchip-tower.pkchip-tower-over{
    position:absolute; bottom:0; left:0;
    pointer-events:none; /* decoratif : laisse passer le survol vers .pkchip-cluster (infobulle) */
  }
  .poker-table-center .pkchip-cluster{display:flex; justify-content:center; margin:0 auto 3px auto;}

  /* Grappe du tapis : posee SUR LE BORD du dessin de la table, decalee
     dans le sens horaire par rapport au siege (comme sur une vraie
     table, ou les jetons d'un joueur sont a sa droite). */
  .table-stack-chip{
    position:absolute; transform:translate(-50%,-50%); text-align:center; z-index:2;
  }
  .table-bet-chip{
    position:absolute; transform:translate(-50%,-50%); text-align:center; z-index:3;
  }
  .pot-label{
    display:block; font-size:0.68rem; color:var(--gold-soft); margin-top:2px;
  }

  /* -------- Petits encarts empiles, en haut a droite de la PAGE -------- */
  .corner-stack{
    /* Colle en haut a droite du CONTENU de la page (relatif a <body>, qui
    a position:relative), pas de l'ECRAN : il defile normalement avec le
    reste de la page et disparait donc quand on descend, au lieu de
    rester visible en permanence a l'ecran. */
    position:absolute; top:8px; right:8px; z-index:9999; max-width:98px;
    display:flex; flex-direction:column; gap:5px;
  }
  .corner-box{
    background:rgba(8,34,26,0.96); border:1px solid var(--line);
    border-radius:7px; padding:4px 6px;
  }
  .corner-box h3{
    margin:0 0 2px 0; font-size:0.5rem; color:var(--gold-soft);
    text-transform:uppercase; letter-spacing:0.2px; white-space:nowrap;
  }
  .corner-box table{width:100%; font-size:0.52rem;}
  .corner-box td, .corner-box th{
    padding:1px 2px; border:none; white-space:nowrap;
    overflow:hidden; text-overflow:ellipsis; max-width:44px;
  }
  .corner-box p{margin:0; font-size:0.52rem; line-height:1.35; white-space:nowrap;}
</style>
<script>

function copyBox(id, btnId){
  var el = document.getElementById(id);
  el.select(); el.setSelectionRange(0, 999999);
  navigator.clipboard.writeText(el.value).then(function(){
    var b = document.getElementById(btnId);
    var old = b.innerText; b.innerText = "Copie !";
    setTimeout(function(){ b.innerText = old; }, 1200);
  });
}
function toggleReveal(id){
  document.getElementById(id).classList.toggle('revealed');
}
// Empeche la page de "sauter" en haut a chaque clic sur un bouton d'action :
// on memorise la position de defilement juste avant l'envoi du formulaire,
// puis on la restaure des que la nouvelle page est chargee.
document.addEventListener('submit', function(e){
  try { sessionStorage.setItem('pokerScrollY', String(window.scrollY)); } catch(err){}
}, true);
window.addEventListener('DOMContentLoaded', function(){
  try {
    var y = sessionStorage.getItem('pokerScrollY');
    if (y !== null){
      window.scrollTo(0, parseInt(y, 10));
      sessionStorage.removeItem('pokerScrollY');
    }
  } catch(err){}
});
</script>
"""


BG_DECOR_HTML = """
<div id="bg-decor">
  <div class="bg-chip" style="width:72px;height:72px; top:3%; left:6%;"></div>
  <div class="bg-chip" style="width:52px;height:52px; top:8%; right:8%; --chip-color:var(--gold);"></div>
  <div class="bg-chip" style="width:38px;height:38px; top:14%; left:38%;"></div>
  <div class="bg-chip" style="width:60px;height:60px; top:19%; left:4%; --chip-color:var(--gold);"></div>
  <div class="bg-chip" style="width:34px;height:34px; top:24%; right:20%;"></div>
  <div class="bg-chip" style="width:46px;height:46px; top:29%; right:6%; --chip-color:var(--gold);"></div>
  <div class="bg-chip" style="width:40px;height:40px; top:35%; left:14%;"></div>
  <div class="bg-chip" style="width:56px;height:56px; top:41%; right:10%;"></div>
  <div class="bg-chip" style="width:32px;height:32px; top:46%; left:45%; --chip-color:var(--gold);"></div>
  <div class="bg-chip" style="width:48px;height:48px; top:52%; left:5%;"></div>
  <div class="bg-chip" style="width:38px;height:38px; top:57%; right:16%; --chip-color:var(--gold);"></div>
  <div class="bg-chip" style="width:64px;height:64px; top:62%; right:4%;"></div>
  <div class="bg-chip" style="width:36px;height:36px; top:68%; left:24%; --chip-color:var(--gold);"></div>
  <div class="bg-chip" style="width:50px;height:50px; top:74%; left:6%;"></div>
  <div class="bg-chip" style="width:42px;height:42px; top:79%; right:22%;"></div>
  <div class="bg-chip" style="width:58px;height:58px; top:85%; right:8%; --chip-color:var(--gold);"></div>
  <div class="bg-chip" style="width:34px;height:34px; top:90%; left:10%;"></div>
  <div class="bg-chip" style="width:46px;height:46px; top:95%; left:42%; --chip-color:var(--gold);"></div>
  <div class="bg-chip" style="width:52px;height:52px; top:98%; right:14%;"></div>
</div>
"""


def render_game_tabs():
    idx = _load_index()
    games = idx["games"]
    active = idx.get("active")
    tabs = ""
    for gm in games:
        if _is_satellite_table(gm):
            continue  # table annexe d'un tournoi multi-table : tourne en
                       # arriere-plan, jamais visible/navigable dans l'UI
        if gm.get("table_closed") and gm["id"] != active:
            continue  # table annexe fermee lors d'un equilibrage : plus d'onglet
        cls = " tab-active" if gm["id"] == active else ""
        tabs += (
            f'<a class="game-tab{cls}" href="{url_for("switch_game", gid=gm["id"])}">'
            f'{html.escape(gm["name"])}</a>'
        )
    tabs += f'<a class="game-tab game-tab-new" href="{url_for("new_game_form")}">+ Nouvelle partie</a>'
    return f'<div class="tabs-row">{tabs}</div>'


def layout(title, body, corner_html="", auto_refresh_seconds=None):
    # Note : on renvoie directement le HTML assemble (pas de moteur de
    # template). Auparavant ce bloc passait par Flask render_template_string,
    # qui re-interprete tout le texte en Jinja2 -- y compris "title" et
    # "corner_html", qui peuvent contenir des noms de joueurs/controleurs
    # saisis librement par l'utilisateur. Un nom du type "{{ 7*7 }}" (ou
    # pire) y aurait ete EXECUTE comme du code Jinja (SSTI), ce qui est
    # particulierement sensible vu que le serveur ecoute sur 0.0.0.0. "body"
    # est deja une chaine Python normale ici, donc rien ne nous obligeait a
    # repasser par un moteur de template pour l'inserer.
    safe_title = html.escape(str(title))
    refresh_tag = (
        f'<meta http-equiv="refresh" content="{int(auto_refresh_seconds)}">'
        if auto_refresh_seconds else ""
    )
    return f"""
    <!DOCTYPE html><html lang="fr"><head>
    <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{safe_title}</title>{refresh_tag}{BASE_CSS}
    <script>
      // Conserve la position de defilement d'un rafraichissement (via
      // la balise meta refresh ci-dessus) au suivant : sans ca, chaque
      // rechargement automatique de la page renvoie tout en haut, ce
      // qui donne l'impression que la page "part n'importe ou".
      window.addEventListener('beforeunload', function() {{
        try {{ sessionStorage.setItem('scrollpos_' + location.pathname, window.scrollY); }} catch (e) {{}}
      }});
      window.addEventListener('load', function() {{
        try {{
          var y = sessionStorage.getItem('scrollpos_' + location.pathname);
          if (y !== null) window.scrollTo(0, parseInt(y, 10));
        }} catch (e) {{}}
      }});
    </script>
    </head>
    <body>{BG_DECOR_HTML}{corner_html}<h1>&#9827; Table de poker</h1><div class="sub">{safe_title}</div>
    {render_game_tabs()}
    {body}
    </body></html>
    """


def render_corner_leaderboard():
    """Petit encart (classement general persistant, tous tournois
    confondus). Retourne uniquement le contenu interne (voir
    render_corner_widgets pour le conteneur positionne qui l'englobe)."""
    leaderboard = game.get_leaderboard()
    if not leaderboard:
        return ""
    rows = "".join(
        f"<tr><td>#{i+1}</td><td>{html.escape(str(ctrl))}</td><td>{pts}</td></tr>"
        for i, (ctrl, pts) in enumerate(leaderboard)
    )
    return f"""
    <div class="corner-box">
      <h3>Classement general</h3>
      <table><tr><th>#</th><th>Ctrl</th><th>Pts</th></tr>{rows}</table>
    </div>
    """


def render_cash_counter():
    """Petit encart (uniquement en cash game) : nombre d'IA
    eliminees-rachetees depuis le debut de CETTE session, et meilleur
    score jamais atteint (record persistant, tous cash games
    confondus). Retourne "" hors cash game."""
    if game.game_type != "cash":
        return ""
    best = game.get_cash_best_record()
    return f"""
    <div class="corner-box">
      <h3>Cash game</h3>
      <p>IA eliminees : <b>{game.cash_ai_busts}</b></p>
      <p>Record perso : <b>{best}</b></p>
    </div>
    """


def render_corner_widgets():
    """Empile, dans un seul conteneur positionne en haut a droite de la
    page, tous les petits encarts a afficher (classement general,
    compteur cash game...). Retourne "" si aucun n'a rien a montrer."""
    parts = [render_corner_leaderboard(), render_cash_counter()]
    parts = [p for p in parts if p]
    if not parts:
        return ""
    return f'<div class="corner-stack">{"".join(parts)}</div>'


def _tournament_overview():
    """Calcule, pour le tournoi multi-table auquel appartient la partie
    active (ou seulement pour la table courante si c'est un tournoi
    mono-table / une partie autonome), le nombre total de joueurs encore
    en lice (tapis > 0) toutes tables confondues, le top 5 des plus gros
    tapis (avec le nom de la table d'origine), et le nombre de tables
    encore ouvertes dans ce tournoi (1 si mono-table/partie autonome)."""
    idx = _load_index()
    active_gid = idx.get("active")
    my_entry = next((gm for gm in idx["games"] if gm["id"] == active_gid), None)
    tid = my_entry.get("tournament_id") if my_entry else None

    if not tid:
        tables = [(my_entry["name"] if my_entry else "Table", game)]
    else:
        tables = []
        for gm in idx["games"]:
            if gm.get("tournament_id") != tid or gm.get("table_closed"):
                continue
            g = game if gm["id"] == active_gid else _get_game_instance(gm["id"])
            tables.append((gm["name"], g))

    all_active = []
    for table_name, g in tables:
        for p in g.players:
            if p["stack"] > 0:
                all_active.append({**p, "_table": table_name})

    total_active = len(all_active)
    top5 = sorted(all_active, key=lambda p: -p["stack"])[:5]
    return total_active, top5, len(tables)


# ----------------------------------------------------------------------
# Page d'accueil / configuration
# ----------------------------------------------------------------------

@app.route("/")
def index():
    # La configuration du relais IA est desormais la toute premiere
    # chose vue a l'ouverture de l'app : l'adresse est memorisee d'une
    # fois sur l'autre (pre-remplie), donc dans l'immense majorite des
    # cas il suffit de verifier/confirmer d'un coup d'oeil avant de
    # continuer - mais ca evite d'oublier de la (re)configurer si elle
    # a change (ex : PC redemarre avec une nouvelle IP locale).
    return redirect(url_for("relay_settings"))


@app.route("/games/switch/<gid>")
def switch_game(gid):
    idx = _load_index()
    entry = next((gm for gm in idx["games"] if gm["id"] == gid), None)
    if entry is None or _is_satellite_table(entry):
        # Table annexe (ou id inconnu) : jamais navigable directement,
        # meme via une URL construite a la main. On reste sur la partie
        # active actuelle.
        return redirect(url_for("table_view"))
    set_active_game(gid)
    g = _get_game_instance(gid)
    if not g.players:
        return redirect(url_for("setup"))
    return redirect(url_for("table_view"))


@app.route("/games/new", methods=["GET", "POST"])
def new_game_form():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        existing = [gm["name"] for gm in list_games()]
        if not name:
            name = f"Partie {len(existing) + 1}"
        # Si la partie active actuelle n'est qu'un emplacement vide (aucun
        # joueur configure - par ex. auto-cree par get_active_game() quand
        # le registre etait vide, juste avant l'affichage de cette page),
        # on la renomme et on la reutilise au lieu d'en creer une nouvelle
        # en plus : evite de laisser trainer une partie fantome "Partie 1"
        # inutilisee a cote de celle qu'on vient vraiment de nommer.
        idx = _load_index()
        active_id = idx.get("active")
        active_entry = next((gm for gm in idx["games"] if gm["id"] == active_id), None)
        if (active_entry is not None and not _is_satellite_table(active_entry)
                and not _get_game_instance(active_id).players):
            active_entry["name"] = name
            _save_index(idx)
            gid = active_id
        else:
            gid = create_game(name)
        set_active_game(gid)
        return redirect(url_for("setup"))
    body = f"""
    <div class="card">
      <p>Nom de la nouvelle partie :</p>
      <form method="post">
        <input name="name" placeholder="Ex : Tournoi du dimanche" autofocus>
        <button>Creer cette partie</button>
      </form>
      <a class="btn secondary" href="{url_for('table_view')}">&larr; Annuler</a>
    </div>
    """
    return layout("Nouvelle partie", body)


@app.route("/games/delete/<gid>", methods=["POST"])
def delete_game_route(gid):
    idx = _load_index()
    entry = next((gm for gm in idx["games"] if gm["id"] == gid), None)
    if entry is not None and _is_satellite_table(entry):
        # Table annexe : geree uniquement par le moteur de tournoi
        # (creation/fermeture automatiques), jamais par une action
        # utilisateur directe.
        return redirect(url_for("table_view"))
    no_games_left = delete_game(gid)
    if no_games_left:
        # Plus aucune partie : direction la vraie premiere page de creation
        # d'une nouvelle partie (choix du nom), plutot qu'une partie vide
        # auto-creee et deja nommee par defaut.
        return redirect(url_for("new_game_form"))
    return redirect(url_for("table_view"))


@app.route("/ai_types/add", methods=["POST"])
def do_add_ai_type():
    add_ai_type(request.form.get("name", ""))
    return redirect(url_for("setup"))


@app.route("/ai_types/remove", methods=["POST"])
def do_remove_ai_type():
    remove_ai_type(request.form.get("name", ""))
    return redirect(url_for("setup"))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if request.method == "POST":
        n = int(request.form["n_players"])
        stack = int(request.form["stack"])
        small_blind = int(request.form.get("small_blind", 100))
        big_blind = int(request.form.get("big_blind", 200))
        ante = int(request.form.get("ante", 10))
        hands_per_level = int(request.form.get("hands_per_level", 10))
        game_type = request.form.get("game_type", "tournament").strip()
        if game_type not in ("tournament", "cash"):
            game_type = "tournament"
        num_tables = int(request.form.get("num_tables", 1) or 1)
        num_tables = max(1, min(num_tables, 20))
        if game_type == "cash":
            num_tables = 1  # le cash game est mono-table uniquement (nombre de joueurs fixe)
        name_pool_raw = request.form.get("name_pool", "").strip()
        seat_controllers, human_names = [], {}
        auto_indices = []
        for i in range(n):
            code = request.form.get(f"controller_{i}", "auto").strip()
            if code == "custom":
                # Nouvelle IA saisie librement par l'utilisateur pour ce siege
                custom_name = request.form.get(f"controller_custom_{i}", "").strip()
                code = custom_name if custom_name else f"IA{i+1}"
            if code == "auto" or code == "":
                # Aucun role explicitement choisi pour ce siege : sera
                # rempli juste apres par une IA choisie aleatoirement,
                # en equilibrant le nombre de sieges par IA (voir
                # _assign_balanced_ai_controllers).
                seat_controllers.append(None)
                auto_indices.append(i)
                continue
            seat_controllers.append(code)
            if CONTROLLER_SHORTCUTS.get(code, code).lower() == "humain":
                human_names[i] = request.form.get(f"human_name_{i}", f"Joueur{i+1}")

        _assign_balanced_ai_controllers(seat_controllers, auto_indices)

        # Un seul pool de noms, construit une fois pour tout le tournoi (et
        # non table par table), puis partage et consomme au fil des tables
        # dans spawn_extra_tables : c'est ce qui garantit l'absence de
        # doublon de nom sur l'ensemble du tournoi (jusqu'a 20 tables x 10
        # joueurs = 200 sieges), et non plus seulement au sein d'une table.
        if name_pool_raw:
            shared_name_pool = [x.strip() for x in name_pool_raw.split(",") if x.strip()]
            random.shuffle(shared_name_pool)
        else:
            shared_name_pool = build_default_name_pool()

        game.setup_players_web(
            stack, name_pool_raw, seat_controllers, human_names,
            small_blind=small_blind, big_blind=big_blind, ante=ante,
            hands_per_level=hands_per_level,
            shared_name_pool=shared_name_pool,
            game_type=game_type,
        )
        if num_tables > 1:
            spawn_extra_tables(
                num_tables, n, stack, name_pool_raw, seat_controllers,
                small_blind, big_blind, ante, hands_per_level,
                shared_name_pool=shared_name_pool,
            )

        # Si un relais PC est configure : envoie automatiquement (en
        # arriere-plan, sans bloquer la creation de la partie) le prompt
        # d'instructions + la table de correspondance a chaque IA geant des
        # bots sur "ma table", en plus des boutons "Copier"/"Envoyer"
        # habituels.
        controllers_to_notify = sorted(set(
            p["controller"] for p in game.players if not p["is_human"]
        ))
        if controllers_to_notify:
            setup_message = AI_PROMPT_TEXT.strip() + "\n\n" + game.code_table_text()
            _relay_broadcast_async({c: setup_message for c in controllers_to_notify}, "Prompt+table")

        return redirect(url_for("show_code_table", first="1"))

    ai_types = load_ai_types()
    ai_options_html = "".join(f'<option value="{name}">{name}</option>' for name in ai_types)

    manage_ai_rows = "".join(f"""
      <span class="tag" style="display:inline-flex; align-items:center; gap:6px; margin:2px 4px 2px 0;">
        {name}
        <form method="post" action="{url_for('do_remove_ai_type')}" style="display:inline; margin:0;"
              onsubmit="return confirm('Supprimer definitivement \\'{name}\\' de la liste des IA proposees ?');">
          <input type="hidden" name="name" value="{name}">
          <button type="submit" class="danger" style="padding:1px 7px; font-size:0.7rem; margin:0;">&times;</button>
        </form>
      </span>
    """ for name in ai_types)

    manage_ai_html = f"""
    <div class="card">
      <h2 style="margin-top:0">Gerer les types d'IA proposes (definitif)</h2>
      <p class="sub" style="margin:0 0 8px 0;">
        Ajoutez ou supprimez une IA de cette liste pour modifier durablement
        les choix proposes ci-dessous, sur cette partie comme sur toutes les
        futures parties. Le bouton "+ Nouvelle IA..." sur un siege reste
        disponible pour un usage ponctuel, sans modifier cette liste.
      </p>
      <div style="margin-bottom:10px;">{manage_ai_rows}</div>
      <form method="post" action="{url_for('do_add_ai_type')}" style="display:flex; gap:8px; align-items:flex-end;">
        <input name="name" placeholder="Ex : Mistral, Grok, Llama..." style="flex:1; margin-bottom:0;">
        <button type="submit" style="margin-bottom:0;">Ajouter</button>
      </form>
    </div>
    """

    rows = "".join(f"""
      <div class="card">
        <b>Siege {i+1}</b>
        <select name="controller_{i}" onchange="toggleCustomAI({i})">
          <option value="auto" selected>Auto - IA aleatoire equilibree</option>
          <option value="0">Humain</option>
          {ai_options_html}
          <option value="custom">+ Nouvelle IA (ponctuel)...</option>
        </select>
        <p class="sub" style="margin:4px 0 0 0;">
          "Auto" : si vous ne changez rien, ce siege sera controle par une
          IA choisie au hasard, en repartissant les sieges le plus
          equitablement possible entre les IA de la table.
        </p>
        <label>Si Humain, votre nom :</label>
        <input name="human_name_{i}" placeholder="Votre nom">
        <div id="customai_{i}" style="display:none;">
          <label>Nom de la nouvelle IA (usage ponctuel, pas ajoutee a la liste) :</label>
          <input name="controller_custom_{i}" placeholder="Ex : Mistral, Grok, Llama...">
        </div>
      </div>
    """ for i in range(10))

    preset_cards = "".join(f"""
      <div class="card preset-card" data-preset="{key}"
           data-stack="{p['stack']}" data-sb="{p['small_blind']}"
           data-bb="{p['big_blind']}" data-ante="{p['ante']}" data-hpl="{p['hands_per_level']}"
           onclick="selectPreset('{key}')">
        <b>{p['label']}</b>
        <p class="sub" style="margin:4px 0 0 0;">{p['description']}</p>
        <p class="sub" style="margin:4px 0 0 0;">
          Tapis {p['stack']:,} &middot; Blindes {p['small_blind']}/{p['big_blind']} &middot;
          Ante {p['ante']} &middot; Niveaux tous les {p['hands_per_level']} mains
        </p>
      </div>
    """.replace(",", " ") for key, p in TOURNAMENT_PRESETS.items())

    body = f"""
    {manage_ai_html}
    <form method="post">
      <div class="card">
        <label>Type de partie</label>
        <select name="game_type" id="game_type" onchange="toggleGameType()">
          <option value="tournament" selected>Tournoi (elimination definitive, blindes progressives)</option>
          <option value="cash">Cash game (nombre de joueurs fixe, blindes fixes, IA rachetees automatiquement)</option>
        </select>
        <p class="sub" id="cash_explanation" style="display:none; margin:4px 0 0 0;">
          En cash game, la table garde toujours le meme nombre de joueurs :
          des qu'un siege IA tombe a 0 jeton, il est immediatement rachete
          (nouveau nom, meme IA aux commandes) avec un tapis egal a la
          moyenne de la table. Si c'est VOUS qui etes elimine, la partie
          s'arrete - un compteur affiche alors combien d'IA vous avez vu
          passer avant votre propre elimination (record personnel a battre).
        </p>
      </div>

      <div class="card">
        <label>Nombre de joueurs a la table</label>
        <input type="number" name="n_players" id="n_players" value="7" min="2" max="10"
               onchange="updateSeats()">
      </div>

      <h2>Structure du tournoi</h2>
      <div id="presets">{preset_cards}</div>

      <div class="card" id="num_tables_card">
        <label>Nombre de tables</label>
        <input type="number" name="num_tables" id="num_tables" value="1" min="1" max="20">
        <p class="sub" style="margin:4px 0 0 0;">
          Au-dela de 1, un tournoi multi-table est cree automatiquement :
          chaque table supplementaire recoit le meme nombre de joueurs que
          "ma table" ci-dessous, avec la meme structure (tapis/blindes/ante/
          niveaux), et ses sieges sont repartis automatiquement entre les
          memes IA que celles choisies pour "ma table" (un siege humain y
          est remplace par une de ces IA, puisque vous ne pouvez jouer que
          sur une seule table a la fois). Chaque table apparait ensuite comme
          un onglet separe en haut de la page. (Non disponible en cash game :
          une seule table fixe.)
        </p>
      </div>

      <div class="card">
        <label>Tapis de depart (identique pour tous)</label>
        <input type="number" name="stack" id="stack" value="50000">
        <label>Petite blinde</label>
        <input type="number" name="small_blind" id="small_blind" value="100">
        <label>Grosse blinde</label>
        <input type="number" name="big_blind" id="big_blind" value="200">
        <label>Ante (par joueur)</label>
        <input type="number" name="ante" id="ante" value="10">
        <div id="hands_per_level_row">
          <label>Nombre de mains avant que les blindes/antes doublent</label>
          <input type="number" name="hands_per_level" id="hands_per_level" value="10">
        </div>
      </div>

      <div class="card">
        <label>Liste de noms pour les sieges IA (laisser vide = liste par defaut)</label>
        <textarea name="name_pool" rows="3">{", ".join(DEFAULT_NAME_POOL)}</textarea>
      </div>
      <div id="seats">{rows}</div>
      <button type="submit">Creer la partie</button>
    </form>
    <script>
      function updateSeats(){{
        var n = parseInt(document.getElementById('n_players').value);
        var seats = document.getElementById('seats').children;
        for (var i=0; i<seats.length; i++){{
          seats[i].style.display = (i < n) ? 'block' : 'none';
        }}
      }}
      function toggleCustomAI(i){{
        var sel = document.querySelector('select[name="controller_' + i + '"]');
        var div = document.getElementById('customai_' + i);
        div.style.display = (sel.value === 'custom') ? 'block' : 'none';
      }}
      function selectPreset(key){{
        var cards = document.querySelectorAll('.preset-card');
        var chosen = null;
        cards.forEach(function(c){{
          c.classList.remove('preset-selected');
          if (c.dataset.preset === key){{ chosen = c; c.classList.add('preset-selected'); }}
        }});
        if (!chosen) return;
        document.getElementById('stack').value = chosen.dataset.stack;
        document.getElementById('small_blind').value = chosen.dataset.sb;
        document.getElementById('big_blind').value = chosen.dataset.bb;
        document.getElementById('ante').value = chosen.dataset.ante;
        document.getElementById('hands_per_level').value = chosen.dataset.hpl;
      }}
      function toggleGameType(){{
        var isCash = document.getElementById('game_type').value === 'cash';
        document.getElementById('num_tables_card').style.display = isCash ? 'none' : 'block';
        document.getElementById('hands_per_level_row').style.display = isCash ? 'none' : 'block';
        document.getElementById('cash_explanation').style.display = isCash ? 'block' : 'none';
        if (isCash) {{ document.getElementById('num_tables').value = 1; }}
      }}
      updateSeats();
      toggleGameType();
    </script>
    """
    return layout("Nouvelle partie", body)


AI_PROMPT_TEXT = """Tu vas incarner un ou plusieurs joueurs (bots) dans une partie de Texas Hold'em No-Limit. Plusieurs IA differentes (dont toi) controlent chacune un ou plusieurs bots a cette meme table, face a un joueur humain qui fait lui aussi partie des joueurs. Un script fait office de croupier : il melange, distribue, et te transmettra a chaque main un message contenant l'etat de la table (positions, tapis), l'historique des actions deja jouees, et les cartes chiffrees de TES bots uniquement (jamais celles des autres joueurs).

REGLES IMPERATIVES :

1. NE REVELE JAMAIS l'identite de tes cartes cachees dans ta reponse visible, ni en clair, ni par des indices suffisamment precis pour les deviner (evite par exemple d'ecrire "j'ai un brelan" ou de decrire la force reelle de ta main). Le joueur humain participe a la partie et ne doit jamais disposer d'informations que les autres joueurs n'ont pas. Garde tout raisonnement sur la force de ta main strictement interne a ta reflexion, ne l'ecris jamais dans ta reponse.

2. A chaque tour, ta reponse doit TOUJOURS contenir, precede si besoin de la description d'attitude (regle 3), un bloc UNIQUE que la personne peut copier-coller en un seul clic directement dans le site. Pour constituer ce bloc, tu dois recopier mot pour mot une partie du message que tu viens de recevoir, puis ajouter ta propre action a la toute fin - jamais l'inverse, ne resume rien, ne reformule rien :

- Si tu es PRE-FLOP (le message recu commence par une ligne "Main #... | Blinds .../... | Ante ..."), ton bloc doit commencer PILE a cette ligne "Main #..." et inclure absolument tout ce qui suit dans le message recu (etat de la table, tes cartes chiffrees, actions deja jouees a ce tour), sans rien omettre.

- Si tu es POST-FLOP (le message recu contient une ligne "Joueurs encore en jeu : ..."), ton bloc doit commencer PILE a CETTE ligne "Joueurs encore en jeu : ..." - ignore tout ce qui la precede dans le message (main, table, cartes, rues et actions des tours precedents, qui ne servent plus a rien a ce stade) - et inclure absolument tout ce qui suit (l'annonce de la rue, les actions deja jouees a ce tour), sans rien omettre.

Dans les deux cas, ta propre ligne d'action vient TOUJOURS en toute derniere ligne du bloc, exactement dans ce format :

NomDuBot : Action

(exemples valides : Fold / Check / Call 200 / Raise a 600 / All-in a 3400)

Le site ignore automatiquement, lors du collage, toute ligne qui n'est pas au format "Nom : Action" (les entetes recopiees ne genent donc rien) : inutile de nettoyer le bloc, contente-toi de recopier tel quel a partir de l'ancre indiquee ci-dessus.

3. OPTIONNEL mais bienvenu : juste avant ce bloc (donc clairement AVANT et EN DEHORS de lui), tu peux ajouter une ou deux phrases decrivant l'attitude du joueur au moment de son action (comme un vrai joueur a la table : hesitation, sourire, soupir, air confiant, petite remarque...). Cette attitude peut deliberement NE PAS refleter la vraie force de la main (bluff comportemental) : hesiter avec une main excellente, ou jouer l'assurance avec une main faible. Varie ces attitudes d'une main a l'autre pour ne pas creer de pattern reconnaissable qui trahirait systematiquement tes vraies mains.

4. ATTENTION PARTICULIERE si tu geres PLUSIEURS bots a cette table : chaque bloc que tu recevras rappellera explicitement la liste de TOUS tes bots (avec la mention "(vous)"). Avant de repondre, verifie toujours cette liste et n'oublie AUCUN de tes bots, meme si un seul d'entre eux doit parler a ce tour precis. Une erreur frequente est d'oublier qu'on controle plusieurs joueurs a la fois : relis bien le rappel a chaque main.

5. NE FAIS JAMAIS agir un bot avant que ce ne soit reellement son tour. Le message que tu recois indique toujours qui doit parler en premier : n'ajoute une action que pour CE joueur precis, jamais pour un joueur qui doit parler plus tard dans l'ordre. Si tu geres plusieurs bots dont un seul doit parler a ce moment, ne fais reagir que celui-la, meme si tu geres aussi l'autre. En cas de doute sur l'ordre exact, ne devine pas : demande confirmation plutot que de faire parler un bot hors tour, cela fausse toute la main.

6. N'ecris JAMAIS de ligne d'action pour un nom de joueur qui n'est pas l'un de TES bots (ceux marques "(vous)" dans le rappel que tu recois). Meme si tu penses savoir ce qu'un autre joueur devrait logiquement faire, ce n'est ni ton role ni ta decision a prendre : chaque IA (ou le joueur humain) ne joue que pour ses propres bots. Une ligne d'action au nom d'un joueur qui n'est pas le tien fausse completement la main et cree des incoherences difficiles a corriger. Si le message ne mentionne aucun de tes bots comme devant agir a ce tour, ne produis simplement aucune ligne d'action.

Es-tu prete a commencer ?"""


@app.route("/aiprompt")
def show_ai_prompt():
    body = f"""
    <div class="card">
      <p>Prompt a copier-coller UNE FOIS dans le tout premier message envoye a chaque IA
      geant des bots, pour lui expliquer les regles du jeu et le format attendu.</p>
      <textarea id="promptbox" class="copybox" style="min-height:340px;" readonly>{AI_PROMPT_TEXT}</textarea>
      <button id="copybtn4" onclick="copyBox('promptbox','copybtn4')">Copier</button>
      <form method="post" action="{url_for('do_relay_send_setup')}" style="display:inline;">
        <button type="submit" class="secondary">Envoyer le prompt + la table via le relais</button>
      </form>
      <a class="btn secondary" href="{url_for('table_view') if game.players else url_for('setup')}">&larr; Retour</a>
    </div>
    """
    return layout("Prompt pour les IA", body)


@app.route("/codetable")
def show_code_table():
    text = game.code_table_text()
    first = request.args.get("first")
    intro = ("<p><b>Partie creee.</b> Copiez ce bloc UNE SEULE FOIS dans le tout premier "
              "message envoye a chaque IA geant des bots.</p>") if first else \
             "<p>Table de correspondance de la partie en cours (a ne renvoyer qu'en cas de besoin).</p>"

    players_html = ""
    prompt_link = ""
    if first and game.players:
        rows = "".join(
            f"<tr><td>{p['name']}</td><td>{p['controller']}</td><td>{p['stack']}</td></tr>"
            for p in game.players
        )
        players_html = f"""
        <div class="card">
          <p><b>Joueurs assignes pour cette partie :</b></p>
          <table><tr><th>Nom</th><th>Gere par</th><th>Tapis</th></tr>{rows}</table>
        </div>
        """
        prompt_json = json_module.dumps(AI_PROMPT_TEXT)
        prompt_link = f"""
        <div class="card">
          <p>Pensez aussi a envoyer le <b>prompt d'instructions</b> a chaque IA, en plus de la
          table de correspondance ci-dessous.</p>
          <button id="promptbtn_setup" onclick="copyPromptSetupDirect('promptbtn_setup')">Copier le prompt pour les IA</button>
          <script>
            const AI_PROMPT_TEXT_SETUP_JS = {prompt_json};
            function copyPromptSetupDirect(btnId){{
              navigator.clipboard.writeText(AI_PROMPT_TEXT_SETUP_JS).then(function(){{
                var b = document.getElementById(btnId);
                var old = b.innerText; b.innerText = "Copie !";
                setTimeout(function(){{ b.innerText = old; }}, 1200);
              }});
            }}
          </script>
        </div>
        """

    body = f"""
    {players_html}
    {prompt_link}
    <div class="card">{intro}
      <textarea id="codebox" class="copybox" readonly>{text}</textarea>
      <button id="copybtn" onclick="copyBox('codebox','copybtn')">Copier</button>
      <form method="post" action="{url_for('do_relay_send_setup')}" style="display:inline;">
        <button type="submit" class="secondary">Envoyer le prompt + la table via le relais</button>
      </form>
      <a class="btn secondary" href="{url_for('table_view')}">Continuer vers la table &rarr;</a>
    </div>
    """
    return layout("Table de correspondance", body)


@app.route("/relay_send_setup", methods=["POST"])
def do_relay_send_setup():
    """Renvoie a la demande (bouton "Envoyer... via le relais") le prompt
    d'instructions + la table de correspondance a chaque IA geant des bots.
    Utile si le relais n'etait pas encore configure au moment de la
    creation de la partie (l'envoi automatique n'avait alors rien pu
    faire), ou pour reessayer apres une erreur."""
    controllers_to_notify = sorted(set(
        p["controller"] for p in game.players if not p["is_human"]
    ))
    if controllers_to_notify:
        setup_message = AI_PROMPT_TEXT.strip() + "\n\n" + game.code_table_text()
        _relay_broadcast_async(
            {c: setup_message for c in controllers_to_notify}, "Prompt+table (envoi manuel)"
        )
    return redirect(request.referrer or url_for("show_code_table"))


@app.route("/relay_send_hand", methods=["POST"])
def do_relay_send_hand():
    """Renvoie a la demande (bouton "Renvoyer cette main via le relais") le
    bloc de la main en cours (etat de la table + cartes) a chaque IA geant
    des bots. Utile pour reessayer apres une erreur, ou si le relais a ete
    configure/reconnecte apres que la main ait deja ete distribuee."""
    if getattr(game, "last_blocks", None):
        _relay_broadcast_async(dict(game.last_blocks), "Main en cours (envoi manuel)")
    return redirect(request.referrer or url_for("table_view"))


# ----------------------------------------------------------------------
# Vue principale de la table
# ----------------------------------------------------------------------

@app.route("/table")
def table_view():
    if not game.players:
        return redirect(url_for("setup"))

    _maybe_start_ai_autoplay()

    def compute_ranks(players):
        """Classement par tapis, du plus gros au plus petit (ex-aequo = meme rang)."""
        ordered = sorted(players, key=lambda p: -p["stack"])
        ranks, prev_stack, current_rank = {}, None, 0
        for i, pp in enumerate(ordered):
            if pp["stack"] != prev_stack:
                current_rank = i + 1
            ranks[pp["name"]] = current_rank
            prev_stack = pp["stack"]
        return ranks

    ranks = compute_ranks(game.players)

    def rank_cell(name):
        r = ranks.get(name, "-")
        cls = ' class="rank-lead"' if r == 1 else ""
        return f"<td{cls}>#{r}</td>"

    next_to_act_early = game.to_act[0] if game.to_act else None
    seats = []
    if game.positions:
        for name, pos in sorted(game.positions.items(), key=lambda x: list(game.positions.values()).index(x[1])):
            p = game.find(name)
            seats.append({
                "pos": pos,
                "name": name,
                "controller": p["controller"] if p else "-",
                "stack": p["stack"] if p else "-",
                "is_human": bool(p and p["is_human"]),
                "folded": name in game.folded,
                "all_in": name in game.all_in,
                "is_next": name == next_to_act_early,
                "rank": ranks.get(name, "-"),
            })
    else:
        for p in game.players:
            seats.append({
                "pos": "-",
                "name": p["name"],
                "controller": p["controller"],
                "stack": p["stack"],
                "is_human": p["is_human"],
                "folded": False,
                "all_in": False,
                "is_next": False,
                "rank": ranks.get(p["name"], "-"),
            })
    _chip_unit = chip_unit_value(game)
    poker_table_html = render_poker_table(
        seats,
        pot=game.pot if game.positions else None,
        current_bets=game.current_bets if game.positions else None,
        unit_value=_chip_unit,
        tier_step=chip_tier_step(game, unit_value=_chip_unit),
    )

    board_html = ""
    if game.hole_cards:
        board_html = render_board(game.board, big=True)

    my_cards_html = ""
    human_players = [p for p in game.players if p["is_human"] and p["name"] in game.hole_cards]
    if len(human_players) == 1:
        # Un seul joueur humain : pas besoin de cacher ses propres cartes de lui-meme
        p = human_players[0]
        c1, c2 = game.hole_cards[p["name"]]
        my_cards_html = (
            '<div class="my-cards-row">'
            + render_card(c1, big=True) + render_card(c2, big=True)
            + "</div>"
        )
    elif len(human_players) > 1:
        # Plusieurs joueurs humains (jeu en pass-and-play sur le meme appareil) :
        # chacun voit le dos de ses cartes par defaut, et les revele en tapant
        # dessus, sans recharger la page.
        for p in human_players:
            c1, c2 = game.hole_cards[p["name"]]
            slug = "".join(ch if ch.isalnum() else "_" for ch in p["name"])
            flip_id = f"flip_{slug}"
            my_cards_html += f"""
            <div class="flip-player-block">
              <div class="flip-player-name">{p['name']}</div>
              <div class="flip-outer" id="{flip_id}" onclick="toggleReveal('{flip_id}')">
                <div class="flip-inner">
                  <div class="flip-face card-back-face">&#127183;<span class="flip-hint">Toucher pour voir</span></div>
                  <div class="flip-face card-front-face">
                    {render_card(c1, big=True)}{render_card(c2, big=True)}
                  </div>
                </div>
              </div>
            </div>
            """

    # Recap visuel de l'abattage : cartes + meilleure combinaison de chaque
    # joueur ayant atteint le showdown (n'apparait que si la main est finie
    # ET que plusieurs joueurs sont alles jusqu'au bout, pas en cas de fold).
    showdown_html = ""
    if game.hand_complete and game.board:
        contenders = [n for n in game.hole_cards if n not in game.folded]
        if len(contenders) > 1:
            results = []
            for name in contenders:
                seven = game.hole_cards[name] + game.board
                score, combo = best_hand_with_cards(seven)
                results.append((score, name, combo))
            best_score = max(r[0] for r in results)
            rows = ""
            for score, name, combo in sorted(results, key=lambda r: -r[0][0]):
                is_winner = score == best_score
                hole_cards_html = "".join(render_card(c) for c in game.hole_cards[name])
                combo_cards = "".join(render_card(c) for c in order_cards_for_display(combo))
                win_tag = ' <span class="tag" style="color:var(--gold-soft);">meilleure main</span>' if is_winner else ""
                rows += f"""
                <div style="margin-bottom:14px;">
                  <p style="margin:0 0 4px 0;"><b>{name}</b>{win_tag}</p>
                  <div class="my-cards-row" style="margin:4px 0 8px 0;">{hole_cards_html}</div>
                  <p style="margin:0 0 4px 0;">{describe(score)}</p>
                  <div class="board-row" style="grid-template-columns:repeat(5,1fr); max-width:280px;">{combo_cards}</div>
                </div>
                """
            showdown_html = f"""
            <div class="card">
              <h2 style="margin-top:0">Abattage</h2>
              {rows}
            </div>
            """

    controllers = sorted(set(p["controller"] for p in game.players if not p["is_human"]))
    import json as _json
    blocks_json = _json.dumps(game.last_blocks)
    ai_prompt_json = _json.dumps(AI_PROMPT_TEXT)
    code_table_json = _json.dumps(game.code_table_text())
    # Donnees necessaires cote JS pour calculer automatiquement le montant
    # d'une "Relance minimum" ou d'une mise "taille du pot" (voir les
    # boutons dans le formulaire "Enregistrer une action" plus bas) :
    # les mises deja engagees par chaque joueur sur la rue en cours,
    # l'increment minimum de relance, et le pot actuel.
    current_bets_json = _json.dumps(game.current_bets)
    min_raise_json = _json.dumps(game.min_raise)
    pot_amount_json = _json.dumps(game.pot)
    street_json = _json.dumps(game.street)
    big_blind_json = _json.dumps(game.big_blind)
    controller_links = "".join(
        f'<button class="secondary" id="blockbtn_{c}" onclick="copyBlockDirect(\'{c}\',\'blockbtn_{c}\')">Bloc {c}</button> '
        for c in controllers
    )
    controller_links += '<button class="secondary" id="promptbtn" onclick="copyPromptDirect(\'promptbtn\')">Copier le prompt IA</button> '
    controller_links += '<button class="secondary" id="codetablebtn" onclick="copyCodeTableDirect(\'codetablebtn\')">Copier la table de correspondance</button> '
    controller_links += (
        f'<form method="post" action="{url_for("do_relay_send_hand")}" style="display:inline;">'
        f'<button type="submit" class="secondary">Renvoyer cette main via le relais</button></form> '
    )
    controller_links += f'<script>const AI_BLOCKS = {blocks_json};\nconst AI_PROMPT_TEXT_JS = {ai_prompt_json};\nconst CODE_TABLE_TEXT_JS = {code_table_json};\n'
    controller_links += """
      function copyBlockDirect(controller, btnId){
        var text = AI_BLOCKS[controller];
        if (!text){ alert("Aucun bloc disponible pour " + controller + " pour l'instant."); return; }
        navigator.clipboard.writeText(text).then(function(){
          var b = document.getElementById(btnId);
          var old = b.innerText; b.innerText = "Copie !";
          setTimeout(function(){ b.innerText = old; }, 1200);
        });
      }
      function copyPromptDirect(btnId){
        navigator.clipboard.writeText(AI_PROMPT_TEXT_JS).then(function(){
          var b = document.getElementById(btnId);
          var old = b.innerText; b.innerText = "Copie !";
          setTimeout(function(){ b.innerText = old; }, 1200);
        });
      }
      function copyCodeTableDirect(btnId){
        navigator.clipboard.writeText(CODE_TABLE_TEXT_JS).then(function(){
          var b = document.getElementById(btnId);
          var old = b.innerText; b.innerText = "Copie !";
          setTimeout(function(){ b.innerText = old; }, 1200);
        });
      }
    </script>"""

    next_to_act = game.to_act[0] if game.to_act else None
    _hunl = game.hands_until_next_level()
    level_tag_html = f"<span class='tag'>Prochaine hausse dans {_hunl} main(s)</span>" if _hunl is not None else "<span class='tag'>Cash game (blindes fixes)</span>"
    player_options = "".join(
        f'<option value="{p["name"]}"{" selected" if p["name"] == next_to_act else ""}>{p["name"]}'
        f'{" (prochain a agir)" if p["name"] == next_to_act else ""}</option>'
        for p in game.players
    )

    # Vue d'ensemble du tournoi (nombre de joueurs encore en lice + top 5
    # des plus gros tapis, toutes tables confondues), affichee juste sous la
    # table de poker, a la place de l'ancien classement general (desormais
    # en petit, en haut a droite de la page - voir render_corner_leaderboard).
    # Uniquement visible s'il y a reellement plusieurs tables encore
    # ouvertes dans ce tournoi (sinon aucun interet : c'est la meme
    # information que la table de poker deja affichee juste au-dessus).
    total_active, top5_stacks, num_tables_open = _tournament_overview()
    tournament_overview_html = ""
    if num_tables_open > 1:
        top5_rows = "".join(
            f"<tr><td>#{i+1}</td><td>{p['name']} <span class='tag'>{p['_table']}</span></td><td>{p['stack']}</td></tr>"
            for i, p in enumerate(top5_stacks)
        )
        tournament_overview_html = f"""
        <div class="card">
          <h2 style="margin-top:0">Tournoi en cours</h2>
          <p>Joueurs encore en lice : <b>{total_active}</b></p>
          <table><tr><th>Rang</th><th>Joueur</th><th>Tapis</th></tr>{top5_rows}</table>
        </div>
        """

    tournament_banner = ""
    if game.tournament_over:
        tournament_banner = f"""
        <div class="card" style="border:2px solid var(--gold); text-align:center;">
          <h2 style="margin-top:0; color:var(--gold-soft);">&#127942; Tournoi termine !</h2>
          <p>Vainqueur : <b>{game.winner}</b></p>
        </div>
        """
    elif game.cash_over:
        best = game.get_cash_best_record()
        record_line = (
            "<p>&#127775; Nouveau record personnel !</p>"
            if game.cash_new_record
            else f"<p>Record personnel a battre : <b>{best}</b></p>"
        )
        tournament_banner = f"""
        <div class="card" style="border:2px solid var(--gold); text-align:center;">
          <h2 style="margin-top:0; color:var(--gold-soft);">Cash game termine</h2>
          <p>Vous avez ete elimine apres avoir vu <b>{game.cash_ai_busts}</b> IA se faire eliminer.</p>
          {record_line}
        </div>
        """

    last_action = last_action_line(game.action_log)
    last_action_html = (
        f'<p class="last-action-banner">{html.escape(last_action)}</p>' if last_action else ""
    )

    action_form_html = f"""
    <div class="card">
      <h2 style="margin-top:0">Enregistrer une action</h2>
      <form method="post" action="{url_for('do_action')}">
        <label>Joueur</label>
        <select name="name" id="actionPlayerSelect" onchange="updateToCallText()">{player_options}</select>
        <p id="toCallText" class="to-call-text"></p>
        <div class="row" style="margin:6px 0 14px 0;">
          <button type="submit" name="action" value="fold" class="danger" formnovalidate>Fold</button>
          <button type="submit" name="action" value="check" class="secondary" formnovalidate>Check</button>
          <button type="submit" name="action" value="call" class="secondary" formnovalidate>Call</button>
          <button type="submit" name="action" value="allin" class="secondary" formnovalidate>All-in</button>
        </div>
        <label>Montant total de la mise (pour Raise/Bet)</label>
        <input type="number" name="amount" id="actionAmountInput" min="1" step="1" required>
        <div class="row" style="margin:6px 0 10px 0;">
          <button type="button" class="secondary" onclick="fillMinRaiseAmount()">Relance minimum</button>
          <button type="button" class="secondary" id="smartBetBtn" onclick="fillSmartBetAmount()">3x BB</button>
          <button type="button" class="secondary" onclick="fillHalfPotBetAmount()">1/2 pot</button>
          <button type="button" class="secondary" onclick="fillPotBetAmount()">Miser le pot</button>
        </div>
        <button type="submit" name="action" value="raise">Raise / Bet</button>
      </form>
      <script>
        const CURRENT_BETS_JS = {current_bets_json};
        const MIN_RAISE_INCREMENT_JS = {min_raise_json};
        const POT_AMOUNT_JS = {pot_amount_json};
        const STREET_JS = {street_json};
        const BIG_BLIND_JS = {big_blind_json};
        function _highestCurrentBetJS(){{
          var highest = 0;
          for (var k in CURRENT_BETS_JS){{ if (CURRENT_BETS_JS[k] > highest) highest = CURRENT_BETS_JS[k]; }}
          return highest;
        }}
        // "Relance minimum" : mise totale minimum autorisee pour une
        // relance, c-a-d la plus haute mise deja engagee sur la rue en
        // cours + l'increment minimum de relance (voir game.min_raise
        // cote Python, qui vaut la grosse blinde en debut de rue).
        function fillMinRaiseAmount(){{
          var name = document.getElementById('actionPlayerSelect').value;
          var already = CURRENT_BETS_JS[name] || 0;
          var total = _highestCurrentBetJS() + MIN_RAISE_INCREMENT_JS;
          document.getElementById('actionAmountInput').value = Math.max(total, already);
        }}
        // Calcule la mise totale correspondant a une fraction donnee du pot
        // (0.5 = 1/2 pot, 1 = pot plein), c-a-d le montant a suivre + cette
        // fraction du pot une fois ce call effectue. fraction=1 reproduit
        // exactement l'ancien comportement de "Miser le pot".
        function _potFractionBetAmount(fraction){{
          var name = document.getElementById('actionPlayerSelect').value;
          var already = CURRENT_BETS_JS[name] || 0;
          var toCall = Math.max(0, _highestCurrentBetJS() - already);
          var potAfterCall = POT_AMOUNT_JS + toCall;
          return already + toCall + Math.round(fraction * potAfterCall);
        }}
        function fillHalfPotBetAmount(){{
          document.getElementById('actionAmountInput').value = _potFractionBetAmount(0.5);
        }}
        function fillPotBetAmount(){{
          document.getElementById('actionAmountInput').value = _potFractionBetAmount(1);
        }}
        // Bouton "intelligent" qui s'adapte au contexte :
        // - preflop, tant qu'aucune relance n'a encore ete faite (la mise
        //   la plus haute sur la table = la grosse blinde, donc seuls les
        //   blinds ont parle) : propose une ouverture standard a 3x la BB.
        // - des qu'une relance a deja eu lieu (preflop) ou des que l'on est
        //   post-flop : propose 1/3 de pot, un sizing de relance/mise plus
        //   adapte a ce contexte.
        function _smartBetIsOpenRaise(){{
          return STREET_JS === "preflop" && _highestCurrentBetJS() <= BIG_BLIND_JS;
        }}
        function fillSmartBetAmount(){{
          if (_smartBetIsOpenRaise()){{
            var name = document.getElementById('actionPlayerSelect').value;
            var already = CURRENT_BETS_JS[name] || 0;
            document.getElementById('actionAmountInput').value = Math.max(3 * BIG_BLIND_JS, already);
          }} else {{
            document.getElementById('actionAmountInput').value = _potFractionBetAmount(1/3);
          }}
        }}
        function _updateSmartBetLabel(){{
          document.getElementById('smartBetBtn').textContent = _smartBetIsOpenRaise() ? "3x BB" : "1/3 pot";
        }}
        // Rappelle, en texte, ce que le joueur actuellement selectionne doit
        // encore suivre sur la rue en cours (0 = peut checker) : evite de
        // devoir calculer la difference mentalement avant de cliquer Call.
        function updateToCallText(){{
          var name = document.getElementById('actionPlayerSelect').value;
          var already = CURRENT_BETS_JS[name] || 0;
          var toCall = Math.max(0, _highestCurrentBetJS() - already);
          var el = document.getElementById('toCallText');
          el.textContent = toCall > 0
            ? "A suivre : " + toCall + " jetons"
            : "Rien a suivre (Check possible)";
        }}
        updateToCallText();
        _updateSmartBetLabel();
      </script>
    </div>
    """

    body = f"""
    {tournament_banner}
    <div class="card">
      <h2 style="margin-top:0">Main #{game.hand_no} &mdash; {game.street}</h2>
      <div class="row">
        <span class="tag">Pot : {game.pot}</span>
        <span class="tag">Blinds {game.small_blind}/{game.big_blind}</span>
        <span class="tag">Ante {game.ante}</span>
        {level_tag_html}
        {"<span class='tag'>Prochain a agir : " + next_to_act + "</span>" if next_to_act else ""}
      </div>
      {last_action_html}
      {"<p><b>Board :</b></p>" + board_html if board_html else ""}
      {"<p><b>Vos cartes :</b></p>" + my_cards_html if my_cards_html else ""}
      {poker_table_html}
    </div>

    <div class="card">
      <h2 style="margin-top:0">Journal detaille</h2>
      <pre class="log">{LAST_LOG or "(aucune action recente)"}</pre>
    </div>

    {action_form_html}
    {tournament_overview_html}
    {showdown_html}

    <div class="card">
      <h2 style="margin-top:0">Nouvelle main / rue</h2>
      <form method="post" action="{url_for('do_new_hand')}"><button{" disabled" if (game.tournament_over or game.cash_over) else ""}>Nouvelle main</button></form>
      <form method="post" action="{url_for('do_undo')}"><button class="secondary">&#8617; Annuler la derniere action</button></form>
    </div>

    <div class="card">
      <h2 style="margin-top:0">Coller un bloc d'actions</h2>
      <form method="post" action="{url_for('do_bulk_action')}">
        <textarea name="block" rows="6" placeholder="Nom : Action&#10;Nom : Action..."></textarea>
        <button>Appliquer le bloc</button>
      </form>
    </div>

    <div class="card">
      <h2 style="margin-top:0">Resume de la main (a copier pour les autres IA)</h2>
      <textarea id="summarybox" class="copybox" readonly>{game.hand_summary_text()}</textarea>
      <button id="copybtn3" onclick="copyBox('summarybox','copybtn3')">Copier</button>
    </div>

    <div class="card">
      <details>
        <summary style="cursor:pointer; font-weight:bold;">Actions manuelles / secours (le tour des IA se joue normalement tout seul)</summary>
        <div style="margin-top:14px;">
          {controller_links}
          <a class="btn secondary" href="{url_for('show_tournament_history')}">Historique complet du tournoi</a>
        </div>
        <div style="margin-top:14px;">
          <p>Le tour de chaque IA se declenche normalement automatiquement.
          Ce bouton ne sert qu'a forcer l'essai maintenant si, pour une
          raison quelconque, ca ne s'est pas fait tout seul.</p>
          <form method="post" action="{url_for('do_ai_autoplay')}">
            <button class="secondary">Forcer le tour des IA maintenant</button>
          </form>
          <p style="margin-top:12px;">
            <a class="btn secondary" href="{url_for('relay_settings')}">Configurer le relais IA (PC)</a>
          </p>
        </div>
      </details>
    </div>

    <p>
      <a class="btn secondary" href="{url_for('setup')}">&#8635; Reconfigurer cette partie</a>
      <form method="post" action="{url_for('delete_game_route', gid=_load_index()['active'])}"
            style="display:inline;" onsubmit="return confirm('Supprimer definitivement cette partie ?');">
        <button class="danger">Supprimer cette partie</button>
      </form>
    </p>
    """
    any_waiting = any(status == "waiting" for status in RELAY_STATUS.values())
    return layout(
        f"Main #{game.hand_no}", body,
        corner_html=render_corner_widgets(),
        auto_refresh_seconds=2 if any_waiting else None,
    )


# ----------------------------------------------------------------------
# Actions (POST)
# ----------------------------------------------------------------------

@app.route("/new_hand", methods=["POST"])
def do_new_hand():
    run_captured(game.new_hand)
    # Si un relais PC est configure : envoie automatiquement (en
    # arriere-plan, sans bloquer l'affichage de la nouvelle main) a chaque
    # IA geant des bots le bloc de sa main (etat de la table + SES cartes
    # chiffrees).
    if getattr(game, "last_blocks", None):
        _relay_broadcast_async(dict(game.last_blocks), "Nouvelle main (cartes)")
    _maybe_start_ai_autoplay()
    return redirect(url_for("table_view"))


@app.route("/undo", methods=["POST"])
def do_undo():
    run_captured(game.undo_last_action)
    return redirect(url_for("table_view"))


@app.route("/tournament_history")
def show_tournament_history():
    text = game.full_tournament_text()
    body = f"""
    <div class="card">
      <p>Historique complet du tournoi (toutes les mains deja terminees).</p>
      <textarea id="histbox" class="copybox" style="min-height:380px;" readonly>{text}</textarea>
      <button id="copybtn5" onclick="copyBox('histbox','copybtn5')">Copier</button>
      <a class="btn secondary" href="{url_for('table_view')}">&larr; Retour</a>
    </div>
    """
    return layout("Historique du tournoi", body)


@app.route("/next_street", methods=["POST"])
def do_next_street():
    run_captured(game.next_street)
    _maybe_start_ai_autoplay()
    return redirect(url_for("table_view"))


def _sync_and_balance_after_hand(before_eliminations, before_hand_complete):
    """A appeler apres toute sequence susceptible d'avoir termine une main
    (showdown explicite OU showdown automatique declenche en interne par
    check_auto_progress(), par ex. quand tout le monde part tapis avant la
    river et qu'aucun bouton "Showdown" n'est jamais clique). Sans cet appel
    systematique, une main terminee automatiquement ne synchronise ni
    n'equilibre jamais les tables annexes du tournoi.

    before_hand_complete : la valeur de game.hand_complete AVANT l'action/le
    showdown qu'on vient de traiter. Sert a detecter qu'une main vient
    JUSTE de se terminer a l'instant (transition False -> True), et non
    qu'elle etait deja terminee avant cet appel (auquel cas apply_action()
    n'a de toute facon rien fait). C'est ce qui declenche, une seule fois
    par main, a la fois le mouvement de pot ET la synchronisation des
    eliminations sur les tables annexes - meme quand aucune elimination
    n'a eu lieu sur la table principale (voir _sync_satellite_pot_movement)."""
    hand_just_completed = game.hand_complete and not before_hand_complete
    if not hand_just_completed:
        return
    # Ne synchronise les tables annexes que depuis LA table ou vous jouez
    # (au moins un siege humain) : c'est elle qui fait foi.
    if not any(p.get("is_human") for p in game.players):
        return

    new_eliminations = len(game.eliminations) - before_eliminations
    if new_eliminations > 0:
        _sync_satellite_eliminations(new_eliminations)

    # game.pot n'est remis a zero qu'au lancement de la main suivante (voir
    # new_hand()) : juste apres un showdown, il contient donc encore le pot
    # final de la main qui vient de se terminer.
    _sync_satellite_pot_movement(game.pot)

    # Le mouvement de pot ci-dessus peut lui-meme avoir elimine des joueurs
    # sur une table annexe : on requilibre donc dans tous les cas, pas
    # seulement quand new_eliminations > 0.
    _balance_satellite_tables()


@app.route("/showdown", methods=["POST"])
def do_showdown():
    before = len(game.eliminations)
    before_complete = game.hand_complete
    run_captured(lambda: (game.showdown(), _sync_and_balance_after_hand(before, before_complete)))
    _maybe_start_ai_autoplay()
    return redirect(url_for("table_view"))


@app.route("/action", methods=["POST"])
def do_action():
    name = request.form["name"]
    action = request.form["action"]
    amount = request.form.get("amount")
    amount = int(amount) if amount else None
    if action in ("raise", "bet") and amount is None:
        # Filet de securite : le formulaire empeche deja normalement ce cas
        # (champ montant obligatoire pour Raise/Bet), mais si amount est
        # quand meme absent (appel API direct, etc.), on ne doit surtout
        # PAS laisser apply_action() tomber sur son input() de secours
        # prevu pour la console : depuis une requete web, ce input() reste
        # bloque indefiniment en attente d'une saisie sur le terminal du
        # serveur, ce qui gele la requete (et le serveur, en mode mono-
        # thread par defaut).
        global LAST_LOG
        LAST_LOG = f"ERREUR : montant manquant pour l'action Raise/Bet de {name}. Action ignoree."
        return redirect(url_for("table_view"))
    before = len(game.eliminations)
    before_complete = game.hand_complete
    run_captured(game.apply_action, name, action, amount)
    # apply_action() peut avoir declenche un showdown automatique en interne
    # (check_auto_progress) sans jamais passer par la route /showdown.
    _sync_and_balance_after_hand(before, before_complete)
    _maybe_start_ai_autoplay()
    return redirect(url_for("table_view"))


def apply_action_lines(text):
    """Parcourt un bloc de texte (reponse d'IA, ou texte colle a la
    main) et applique toute ligne reconnue au format "Nom : Action".
    Les lignes qui ne correspondent pas a ce format (attitude du
    joueur, texte d'introduction, entetes recopiees...) sont
    silencieusement ignorees. Retourne la liste des (nom, action,
    montant) effectivement appliques, pour permettre de detecter si
    le bloc n'a produit aucune action exploitable."""
    applied = []
    for line in text.splitlines():
        parsed = game.parse_action_line(line)
        if parsed is None:
            continue
        name, action, amount = parsed
        if action in ("raise", "bet") and amount is None:
            # Meme garde-fou que do_action() : sans ca, apply_action()
            # tomberait sur son input() de secours et gelerait la requete.
            print(f"ERREUR : montant manquant pour l'action Raise/Bet de {name} "
                  f"(ligne '{line}'). Action ignoree.")
            continue
        game.apply_action(name, action, amount)
        applied.append((name, action, amount))
    game.save()
    return applied


@app.route("/bulk_action", methods=["POST"])
def do_bulk_action():
    block = request.form.get("block", "")
    before = len(game.eliminations)
    before_complete = game.hand_complete
    run_captured(apply_action_lines, block)
    _sync_and_balance_after_hand(before, before_complete)
    _maybe_start_ai_autoplay()
    return redirect(url_for("table_view"))


@app.route("/relay_settings", methods=["GET", "POST"])
def relay_settings():
    if request.method == "POST":
        url = request.form.get("relay_url", "").strip()
        save_relay_config({"relay_url": url})
        return redirect(url_for("relay_settings"))

    cfg = load_relay_config()
    current = html.escape(cfg.get("relay_url", ""))
    continue_url = url_for("table_view") if game.players else url_for("setup")
    body = f"""
    <div class="card">
      <h2 style="margin-top:0">Relais IA (PC)</h2>
      <p>Renseigne ici l'adresse du petit serveur qui tourne sur ton PC
      (voir le dossier <code>relay/</code> fourni avec l'app). En general
      une adresse locale du style <code>http://192.168.1.XX:8765</code>,
      trouvable via l'IP affichee au demarrage du serveur sur ton PC.
      Ton telephone et ton PC doivent etre sur le meme reseau Wi-Fi.</p>
      <p>Cette adresse est memorisee : verifie/modifie-la si besoin (par
      exemple si ton PC a change d'adresse locale), puis continue.</p>
      <form method="post">
        <input type="text" name="relay_url" value="{current}"
               placeholder="http://192.168.1.XX:8765" style="width:100%;">
        <button style="margin-top:12px;">Enregistrer</button>
      </form>
      <p style="margin-top:20px;">
        <a class="btn" href="{continue_url}">Continuer vers ma partie &rarr;</a>
      </p>
    </div>
    """
    return layout("Relais IA", body)


def _run_ai_autoplay_loop():
    """Fait jouer automatiquement, via le relais PC, tous les bots IA
    dont c'est le tour, a la suite, jusqu'a ce que ce soit au tour
    d'un joueur humain, que la main soit terminee, ou qu'une erreur
    survienne (relais injoignable, reponse non exploitable...). Le
    detail de chaque echange (message envoye, reponse brute recue) est
    ajoute au journal pour que l'utilisateur puisse comprendre et,
    au besoin, corriger a la main via le bloc de collage habituel."""
    global LAST_LOG
    log_parts = []
    max_iterations = 8  # garde-fou : evite une boucle infinie en cas de souci

    for _ in range(max_iterations):
        if game.hand_complete or not game.to_act:
            break
        name = game.to_act[0]
        player = next((p for p in game.players if p["name"] == name), None)
        if player is None or player.get("is_human"):
            break

        ai_type = player["controller"]
        message = game.hand_summary_text()
        reply, error = ask_relay_for_action(ai_type, message)
        if error:
            log_parts.append(f"[{ai_type} / {name}] ERREUR : {error}")
            break

        log_parts.append(f"[{ai_type} / {name}] a repondu :\n{reply}")

        before = len(game.eliminations)
        before_complete = game.hand_complete
        applied = apply_action_lines(reply)
        _sync_and_balance_after_hand(before, before_complete)

        if not any(a[0] == name for a in applied):
            log_parts.append(
                f"ATTENTION : aucune action reconnue pour {name} dans la reponse "
                f"ci-dessus. Verifie/complete a la main avec le bloc de collage."
            )
            break

    with _relay_log_lock:
        LAST_LOG = "\n\n".join(log_parts) if log_parts else "(aucune IA n'avait a jouer)"


@app.route("/ai_autoplay", methods=["POST"])
def do_ai_autoplay():
    """Bouton de secours : force le declenchement immediat du tour des
    IA. Les tours des IA se declenchent normalement tout seuls (voir
    _maybe_start_ai_autoplay) - ce bouton n'est utile que si, pour une
    raison quelconque, ce declenchement automatique n'a pas eu lieu."""
    with _autoplay_lock:
        if not _autoplay_running:
            _start_autoplay_worker()
    return redirect(url_for("table_view"))


if __name__ == "__main__":
    print("Ouvrez votre navigateur sur : http://127.0.0.1:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
