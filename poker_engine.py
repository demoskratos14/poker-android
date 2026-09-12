#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MOTEUR DE POKER - Texas Hold'em (No-Limit)
===========================================
Compatible Pydroid 3 (aucune dependance externe).

Ce script joue le role de "croupier + greffier" :
- Il melange et distribue les cartes.
- Il chiffre les mains des joueurs geres par une IA (codes aleatoires,
  regeneres a CHAQUE main, jamais affiches en clair a l'ecran).
- Il affiche en clair UNIQUEMENT la main du/des joueur(s) marques
  comme "humain" (vous) et les cartes communes (flop/turn/river),
  qui sont publiques.
- Il calcule pot, tapis, positions, blinds/antes, side-pots et
  determine le(s) gagnant(s) a l'abattage.

Utilisation typique :
1. Lancez le script -> menu "1. Nouvelle partie" pour creer la table.
2. A chaque main : "2. Nouvelle main" -> distribue et affiche les
   blocs a copier-coller a chaque IA (codes chiffres) + votre main
   en clair.
3. Pour chaque tour d'encheres, utilisez "3. Enregistrer une action"
   autant de fois que necessaire, puis "4. Passer a la rue suivante"
   pour reveler flop/turn/river.
4. A l'abattage : "5. Showdown / distribuer le pot".
5. "6. Etat de la table" a tout moment pour un recapitulatif.
6. "0. Quitter" (l'etat est sauvegarde automatiquement).

IMPORTANT : ne modifiez pas les fonctions qui impriment les mains des
joueurs IA en clair, sinon vous perdriez la parite d'information avec
les autres joueurs (c'est tout l'interet du systeme).
"""

import random
import json
import os
import re
import string
import time
import contextlib
from itertools import combinations
from collections import Counter

SAVE_FILE = "poker_state.json"

# Fichier de classement general, PARTAGE entre toutes les parties/onglets
# (contrairement a SAVE_FILE qui est propre a chaque partie). Chaque
# controleur (IA ou humain) y accumule ses points au fil des tournois.
LEADERBOARD_FILE = "poker_leaderboard.json"

# Fichier-verrou associe, utilise pour serialiser les sequences
# lecture-modification-ecriture du classement entre parties/onglets
# concurrents (voir _leaderboard_lock ci-dessous).
LEADERBOARD_LOCK_FILE = LEADERBOARD_FILE + ".lock"

# Fichier du meilleur score cash game jamais atteint (nombre d'IA
# eliminees-rachetees par le joueur humain avant sa propre elimination),
# PARTAGE entre toutes les parties, comme LEADERBOARD_FILE.
CASH_RECORD_FILE = "poker_cash_best.json"


@contextlib.contextmanager
def _leaderboard_lock(timeout=10.0, poll=0.05):
    """Verrou inter-processus portable (aucune dependance externe, donc
    compatible Pydroid 3) protegeant le classement general contre les
    ecritures concurrentes.

    S'appuie sur la creation EXCLUSIVE d'un fichier ".lock" : sur la
    plupart des OS (dont Android/Linux), os.open(..., O_CREAT | O_EXCL)
    est une operation atomique fournie par le systeme de fichiers, donc
    un seul appelant a la fois peut reussir a creer ce fichier. Les
    autres attendent (poll) jusqu'a obtenir le verrou ou expiration du
    delai ``timeout``, auquel cas on suppose un verrou "orphelin" laisse
    par un crash precedent et on le force.
    """
    deadline = time.time() + timeout
    fd = None
    while fd is None:
        try:
            fd = os.open(LEADERBOARD_LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.time() > deadline:
                # Verrou probablement orphelin (processus tue en cours
                # d'ecriture) : on le retire et on reessaie.
                try:
                    os.remove(LEADERBOARD_LOCK_FILE)
                except OSError:
                    pass
                deadline = time.time() + timeout
            else:
                time.sleep(poll)
    try:
        yield
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.remove(LEADERBOARD_LOCK_FILE)
        except OSError:
            pass

# Liste par defaut des noms disponibles pour les sieges geres par une IA.
# Modifiable directement ici (une fois pour toutes), ou a la volee au
# lancement d'une nouvelle partie (option 1 du menu).
#
# PRIORITY_NAME_POOL : les 15 noms "historiques", utilises en priorite.
# EXTRA_NAME_POOL : 184 noms supplementaires (autres joueurs de poker
# connus), utilises seulement une fois les 15 premiers deja distribues.
# Avec les deux listes combinees (200 noms), un tournoi de 20 tables x 10
# joueurs (200 sieges, moins 1 joueur humain) peut etre rempli sans le
# moindre doublon de nom.
PRIORITY_NAME_POOL = [
    "Phil Ivey", "Doyle Branson", "Phill Hellmuth", "Daniel Negreanu",
    "Dario Minery", "Bertrand Gropelier", "Faraz Jaka", "Patrik Antonius",
    "Chris Moneymaker", "Antonio Esfandiari", "Barry Greenstein",
    "Gus Hansen", "Jason Mercier", "Scotty Nguyen", "Vanessa Selbst",
]

EXTRA_NAME_POOL = [
    "Johnny Chan", "Stu Ungar", "Amarillo Slim", "Puggy Pearson",
    "Chip Reese", "Erik Seidel", "Huck Seed", "Men Nguyen",
    "John Juanda", "David Chiu", "Carlos Mortensen", "Greg Raymer",
    "Joe Hachem", "Jamie Gold", "Jerry Yang", "Peter Eastgate",
    "Joe Cada", "Pius Heinz", "Greg Merson", "Ryan Riess",
    "Martin Jacobson", "Joe McKeehen", "Qui Nguyen", "Scott Blumstein",
    "John Cynn", "Hossein Ensan", "Damian Salas", "Koray Aldemir",
    "Espen Jorstad", "Daniel Weinman", "Jonathan Tamayo", "Phil Laak",
    "Jennifer Harman", "Annie Duke", "Howard Lederer", "Chris Ferguson",
    "Ted Forrest", "Layne Flack", "David Ulliott", "Tony G",
    "Marcel Luske", "Isabelle Mercier", "Liv Boeree", "Kara Scott",
    "Kristen Bicknell", "Maria Ho", "Jennifer Tilly", "Loni Harwood",
    "Vanessa Rousso", "Kathy Liebert", "Cyndy Violette", "Barbara Enright",
    "Jesse Sylvia", "Sam Trickett", "Tom Dwan", "Phil Galfond",
    "Dan Cates", "Viktor Blom", "Fedor Holz", "Bryn Kenney",
    "Justin Bonomo", "Steve O'Dwyer", "Stephen Chidwick", "Adrian Mateos",
    "Dan Smith", "David Peters", "Sam Greenwood", "Ike Haxton",
    "Timothy Adams", "Rainer Kempe", "Christoph Vogelsang", "Nick Petrangelo",
    "Seth Davies", "Alex Foxen", "Kahle Burns", "Ben Tollerene",
    "Ben Lamb", "Brian Rast", "Cary Katz", "Bill Perkins",
    "Andrew Robl", "Brian Hastings", "Tom Marchese", "Sorel Mizzi",
    "Olivier Busquet", "Scott Seiver", "Matt Glantz", "Todd Brunson",
    "Chino Rheem", "J.C. Tran", "Jean-Robert Bellande", "Mike Matusow",
    "Mike Sexton", "Amir Lehavot", "Tony Dunst", "Norman Chad",
    "Lon McEachern", "Vince Van Patten", "William Kassouf", "Melanie Weisner",
    "Shaun Deeb", "Chris Moorman", "Ali Imsirovic", "Andras Nemeth",
    "Manig Loeser", "Michael Addamo", "Wai Kin Yong", "Jans Arends",
    "Mikita Badziakouski", "Chance Kornuth", "Cate Hall", "Ari Engel",
    "Ryan Laplante", "Doug Polk", "Ben Sulsky", "Ryan Fee",
    "Andrew Neeme", "Brad Owen", "Marle Cordeiro", "Lex Veldhuis",
    "Jason Somerville", "Jaime Staples", "Parker Talbot", "Bill Klein",
    "Rob Yong", "Talal Shakerchi", "Elton Tsang", "Paul Phua",
    "Richard Yong", "Wesley Fei", "Leon Tsoukernik", "Isaac Baron",
    "David Benyamine", "Guy Laliberte", "Xuan Liu", "Nolan Dalla",
    "Linda Johnson", "Mori Eskandani", "Gabe Kaplan", "James Woods",
    "Rafe Furst", "Andy Bloch", "Perry Friedman", "Susie Isaacs",
    "Marsha Waggoner", "Melissa Burr", "Jan Fisher", "Freddy Deeb",
    "Humberto Brenes", "David Baker", "Allen Cunningham", "John Hennigan",
    "Eli Elezra", "Josh Arieh", "David Williams", "Steve Zolotow",
    "Michael Mizrachi", "Robert Mizrachi", "Matt Savage", "Jack Binion",
    "Bobby Baldwin", "Tom McEvoy", "Berry Johnston", "Hal Fowler",
    "Brad Daugherty", "Dan Harrington", "Robert Varkonyi", "Chris Bjorin",
    "Ylon Schwartz", "Chad Brown", "Mark Newhouse", "Bruno Politano",
    "Felipe Ramos", "Yueqi Zhu", "Kelly Minkin", "Nick Schulman",
    "Jake Cody", "Sofia Lovgren", "Charlie Carrel", "Patrick Leonard",
    "Fintan Gavin", "Roberto Romanello", "Toby Lewis", "Niall Farrell",
    "Jack Salter", "James Akenhead", "Praz Bansi", "Sam Grafton",
    "James Hartigan", "Anthony Zinno", "Cliff Josephy", "Gordon Vayo",
    "Neil Blumenfield", "Kenny Hallaert", "Felix Stephensen", "Griffin Benger",
]

# Liste complete (200 noms), les 15 prioritaires d'abord. Conservee pour
# retro-compatibilite avec le code existant qui reference DEFAULT_NAME_POOL
# (ex. affichage de la liste par defaut dans le menu console).
DEFAULT_NAME_POOL = PRIORITY_NAME_POOL + EXTRA_NAME_POOL


def build_default_name_pool():
    """Construit la file d'attente des noms par defaut a piocher pour les
    sieges IA, dans l'ordre ou ils seront distribues (le prochain nom pioche
    est toujours pool[0]).

    Les 15 noms de PRIORITY_NAME_POOL sont toujours distribues avant les 184
    de EXTRA_NAME_POOL (mais leur ordre interne est aleatoire, pour ne pas
    toujours voir "Phil Ivey" en premier). On ne pioche dans EXTRA_NAME_POOL
    que si les 15 prioritaires ont deja tous ete attribues."""
    priority = list(PRIORITY_NAME_POOL)
    extra = list(EXTRA_NAME_POOL)
    random.shuffle(priority)
    random.shuffle(extra)
    return priority + extra

# Raccourcis numeriques pour designer le controleur d'un siege a la creation
# de la partie (option 1 du menu). "0" reste reserve au joueur humain.
CONTROLLER_SHORTCUTS = {
    "0": "Humain",
    "1": "Claude",
    "2": "Vibe",
    "3": "ChatGPT",
}

RANKS = "23456789TJQKA"
RANK_NAMES = {"2":"2","3":"3","4":"4","5":"5","6":"6","7":"7","8":"8",
              "9":"9","T":"10","J":"Valet","Q":"Dame","K":"Roi","A":"As"}
SUITS = {"S": "\u2660", "H": "\u2665", "D": "\u2666", "C": "\u2663"}  # ♠ ♥ ♦ ♣
RANK_VALUE = {r: i + 2 for i, r in enumerate(RANKS)}

HAND_NAMES = {
    8: "Quinte flush", 7: "Carre", 6: "Full", 5: "Couleur",
    4: "Quinte", 3: "Brelan", 2: "Deux paires", 1: "Paire", 0: "Carte haute"
}

# Etiquettes de position standard selon le nombre de joueurs actifs a table
POSITION_LABELS = {
    2: ["BTN/SB", "BB"],
    3: ["BTN", "SB", "BB"],
    4: ["BTN", "SB", "BB", "UTG"],
    5: ["BTN", "SB", "BB", "UTG", "CO"],
    6: ["BTN", "SB", "BB", "UTG", "HJ", "CO"],
    7: ["BTN", "SB", "BB", "UTG", "UTG+1", "HJ", "CO"],
    8: ["BTN", "SB", "BB", "UTG", "UTG+1", "MP", "HJ", "CO"],
    9: ["BTN", "SB", "BB", "UTG", "UTG+1", "UTG+2", "MP", "HJ", "CO"],
    10: ["BTN", "SB", "BB", "UTG", "UTG+1", "UTG+2", "MP1", "MP2", "HJ", "CO"],
}


def position_labels_for(n):
    if n in POSITION_LABELS:
        return POSITION_LABELS[n]
    # repli generique si plus de 10 joueurs (improbable)
    base = ["BTN", "SB", "BB"]
    extra = [f"UTG+{i}" for i in range(n - 3)]
    return base + extra


# ----------------------------------------------------------------------
# Cartes / Deck / Codes chiffres
# ----------------------------------------------------------------------

def ask_int(prompt):
    """Comme int(input(prompt)), mais redemande au lieu de planter toute la
    session console si la saisie n'est pas un nombre entier valide."""
    while True:
        raw = input(prompt).strip()
        try:
            return int(raw)
        except ValueError:
            print(f"Valeur invalide ({raw!r}) : veuillez saisir un nombre entier.")


def make_deck():
    return [(r, s) for s in SUITS for r in RANKS]


def card_label(card):
    r, s = card
    return f"{RANK_NAMES[r]}{SUITS[s]}"


def random_code(existing, length=4):
    alphabet = string.ascii_letters + string.digits
    while True:
        code = "".join(random.choice(alphabet) for _ in range(length))
        if code not in existing:
            return code


def generate_code_table():
    """Genere UNE table fixe code<->carte pour toute la duree du tournoi.
    Cette table doit etre communiquee UNE SEULE FOIS a chaque IA au debut
    de la partie (comme la table manuelle initiale). Elle ne change plus
    ensuite : seules les cartes reellement distribuees varient a chaque
    main (vrai melange aleatoire du script)."""
    deck = make_deck()
    used = set()
    table = {}
    for card in deck:
        code = random_code(used)
        used.add(code)
        table[card] = code
    return table


# ----------------------------------------------------------------------
# Evaluation des mains (7 cartes -> meilleure combinaison de 5)
# ----------------------------------------------------------------------

def evaluate_5(cards):
    ranks = sorted([RANK_VALUE[c[0]] for c in cards], reverse=True)
    suits = [c[1] for c in cards]
    counts = Counter(ranks)
    ordered = sorted(counts.items(), key=lambda x: (-x[1], -x[0]))
    is_flush = len(set(suits)) == 1
    uniq = sorted(set(ranks), reverse=True)

    is_straight, straight_high = False, None
    if len(uniq) == 5:
        if uniq[0] - uniq[4] == 4:
            is_straight, straight_high = True, uniq[0]
        elif uniq == [14, 5, 4, 3, 2]:
            is_straight, straight_high = True, 5

    if is_straight and is_flush:
        return (8, straight_high)
    if ordered[0][1] == 4:
        four = ordered[0][0]
        kicker = max(r for r in ranks if r != four)
        return (7, four, kicker)
    if ordered[0][1] == 3 and len(ordered) > 1 and ordered[1][1] >= 2:
        return (6, ordered[0][0], ordered[1][0])
    if is_flush:
        return (5,) + tuple(ranks[:5])
    if is_straight:
        return (4, straight_high)
    if ordered[0][1] == 3:
        kickers = sorted([r for r in ranks if r != ordered[0][0]], reverse=True)[:2]
        return (3, ordered[0][0]) + tuple(kickers)
    if ordered[0][1] == 2 and len(ordered) > 1 and ordered[1][1] == 2:
        pairs = sorted([ordered[0][0], ordered[1][0]], reverse=True)
        kicker = max(r for r in ranks if r not in pairs)
        return (2,) + tuple(pairs) + (kicker,)
    if ordered[0][1] == 2:
        pair = ordered[0][0]
        kickers = sorted([r for r in ranks if r != pair], reverse=True)[:3]
        return (1, pair) + tuple(kickers)
    return (0,) + tuple(ranks[:5])


def best_hand(seven_cards):
    return max(evaluate_5(list(c)) for c in combinations(seven_cards, 5))


def best_hand_with_cards(seven_cards):
    """Comme best_hand(), mais renvoie aussi les 5 cartes exactes formant la
    meilleure combinaison (utile pour un affichage visuel a l'abattage)."""
    best_score, best_combo = None, None
    for combo in combinations(seven_cards, 5):
        s = evaluate_5(list(combo))
        if best_score is None or s > best_score:
            best_score, best_combo = s, combo
    return best_score, best_combo


def describe(score):
    return HAND_NAMES[score[0]]


def order_cards_for_display(combo):
    """Range les 5 cartes d'une combinaison comme on le ferait naturellement
    a la main : groupes (brelan/paires/carre) d'abord, puis kickers en ordre
    decroissant. Exception pour la double paire, ou le kicker est place en
    premier (convention demandee)."""
    groups = {}
    for c in combo:
        r = RANK_VALUE[c[0]]
        groups.setdefault(r, []).append(c)
    counts = {r: len(cs) for r, cs in groups.items()}
    shape = sorted(counts.values(), reverse=True)

    if shape == [2, 2, 1]:
        pair_ranks = sorted([r for r, v in counts.items() if v == 2], reverse=True)
        kicker_rank = [r for r, v in counts.items() if v == 1][0]
        return groups[kicker_rank] + groups[pair_ranks[0]] + groups[pair_ranks[1]]
    if shape == [3, 1, 1]:
        trip_rank = [r for r, v in counts.items() if v == 3][0]
        kickers = sorted([r for r, v in counts.items() if v == 1], reverse=True)
        return groups[trip_rank] + groups[kickers[0]] + groups[kickers[1]]
    if shape == [3, 2]:
        trip_rank = [r for r, v in counts.items() if v == 3][0]
        pair_rank = [r for r, v in counts.items() if v == 2][0]
        return groups[trip_rank] + groups[pair_rank]
    if shape == [4, 1]:
        quad_rank = [r for r, v in counts.items() if v == 4][0]
        kicker_rank = [r for r, v in counts.items() if v == 1][0]
        return groups[quad_rank] + groups[kicker_rank]
    if shape == [2, 1, 1, 1]:
        pair_rank = [r for r, v in counts.items() if v == 2][0]
        kickers = sorted([r for r, v in counts.items() if v == 1], reverse=True)
        ordered = list(groups[pair_rank])
        for k in kickers:
            ordered += groups[k]
        return ordered
    # carte haute, couleur, quinte, quinte flush : simple tri decroissant
    return sorted(combo, key=lambda c: -RANK_VALUE[c[0]])


# ----------------------------------------------------------------------
# Etat de la partie
# ----------------------------------------------------------------------

class Game:
    def __init__(self, save_file=None):
        self.save_file = save_file or SAVE_FILE  # permet plusieurs parties distinctes en parallele
        self.players = []       # liste de dicts: name, controller, is_human, stack, active(bool ds la main)
        self.button = 0
        self.hand_no = 0
        self.small_blind = 100
        self.big_blind = 200
        self.ante = 10
        self.hands_per_level = 10   # nombre de mains avant que les blinds/antes doublent
        self.pot = 0
        self.current_bets = {}  # nom -> mise engagee dans la rue en cours
        self.board = []
        self.deck = []
        self.hole_cards = {}    # nom -> [carte, carte]
        self.codes = {}         # nom -> [code, code]  (pour affichage chiffre)
        self.folded = set()
        self.all_in = set()
        self.call_only = set()  # joueurs restreints a suivre/se coucher (relance incomplete subie)
        self.street = "preflop"
        self.code_table = {}    # (rang,couleur) -> code, FIXE pour toute la partie
        self.positions = {}     # nom -> etiquette de position (BTN, SB, BB, UTG...)
        self.last_blocks = {}   # controller -> texte du bloc a copier (main en cours)
        self.to_act = []        # FILE ORDONNEE des joueurs devant agir (le premier de la liste = prochain a parler)
        self.seat_order = []    # ordre des sieges autour de la table pour cette main (BTN en premier)
        self.street_order = []  # ordre de parole pour la rue en cours
        self.hand_complete = False
        self.action_log = []    # historique de la main en cours, format copiable
        self.total_contrib = {} # nom -> total mise dans le pot sur TOUTE la main (pour les side-pots)
        self.min_raise = 0      # taille minimum d'une relance dans la rue en cours
        self.history_stack = [] # piles d'etats precedents, pour "annuler la derniere action"
        self.tournament_log = []  # historique complet du tournoi (une entree par main terminee)
        self.tournament_over = False
        self.winner = None
        self.eliminations = []  # [{"name":..., "hand_no":...}] dans l'ordre d'elimination
        self.ranking_recorded = False  # evite de comptabiliser 2x le meme tournoi au classement
        self.starting_stack = 0       # tapis de depart (identique pour tous), FIXE pour toute la partie
        self.starting_num_players = 0  # nombre de joueurs au demarrage, FIXE pour toute la partie
        # starting_stack * starting_num_players = total de jetons en jeu dans le tournoi,
        # une quantite CONSTANTE (les jetons ne font que circuler entre joueurs/pot, ils ne
        # sont jamais crees ni detruits). Sert de reference FIXE pour l'affichage des grappes
        # de jetons (tapis, mises, pot) cote web, afin qu'un montant donne soit toujours
        # represente par la meme taille, independamment de ce que font les autres joueurs.

        # ---------- mode cash game ----------
        # "tournament" (comportement historique : elimination definitive,
        # hausse progressive des blindes, classement de fin de tournoi) ou
        # "cash" (nombre de joueurs FIXE : tout siege IA qui tombe a 0 est
        # immediatement rachete avec un tapis egal a la moyenne de la table ;
        # blindes fixes ; la partie se termine seulement quand le joueur
        # humain lui-meme est elimine).
        self.game_type = "tournament"
        self.cash_over = False       # True des que le joueur humain est elimine en cash game
        self.cash_ai_busts = 0       # nombre d'IA rachetees depuis le debut de CETTE session cash
        self.cash_new_record = False  # True si cash_ai_busts a strictement depasse le record precedent

        # Pour chaque controleur (IA), index dans self.action_log jusqu'ou
        # son dernier message envoye (via le relais automatique) est deja
        # alle. Sert a ne lui envoyer, a chaque tour suivant, QUE les
        # nouvelles lignes depuis son dernier message sur cette main
        # (voir hand_delta_text) - au lieu de repeter tout l'historique
        # de la main a chaque fois, ce qui gonflerait inutilement la
        # conversation (et donc la consommation de quota) au fil des tours.
        self.sent_log_index = {}

    # ---------- persistance ----------
    def _card_to_list(self, card):
        return [card[0], card[1]]

    def _card_from_list(self, lst):
        return (lst[0], lst[1])

    def save(self):
        # le code_table est serialise avec des cles str "rang_couleur"
        serial_table = {f"{r}_{s}": c for (r, s), c in self.code_table.items()}
        data = {
            "players": self.players, "button": self.button, "hand_no": self.hand_no,
            "small_blind": self.small_blind, "big_blind": self.big_blind, "ante": self.ante,
            "hands_per_level": self.hands_per_level,
            "code_table": serial_table,
            # --- etat complet de la main en cours (pour survivre a un redemarrage) ---
            "pot": self.pot,
            "current_bets": self.current_bets,
            "board": [self._card_to_list(c) for c in self.board],
            "deck": [self._card_to_list(c) for c in self.deck],
            "hole_cards": {n: [self._card_to_list(c) for c in cs] for n, cs in self.hole_cards.items()},
            "codes": self.codes,
            "folded": list(self.folded),
            "all_in": list(self.all_in),
            "call_only": list(self.call_only),
            "street": self.street,
            "positions": self.positions,
            "last_blocks": self.last_blocks,
            "to_act": list(self.to_act),
            "seat_order": self.seat_order,
            "street_order": self.street_order,
            "total_contrib": self.total_contrib,
            "min_raise": self.min_raise,
            "history_stack": self.history_stack,
            "tournament_log": self.tournament_log,
            "tournament_over": self.tournament_over,
            "winner": self.winner,
            "eliminations": self.eliminations,
            "ranking_recorded": self.ranking_recorded,
            "starting_stack": self.starting_stack,
            "starting_num_players": self.starting_num_players,
            "hand_complete": self.hand_complete,
            "action_log": self.action_log,
            "game_type": self.game_type,
            "cash_over": self.cash_over,
            "cash_ai_busts": self.cash_ai_busts,
            "cash_new_record": self.cash_new_record,
            "sent_log_index": self.sent_log_index,
        }
        # Ecriture atomique : on ecrit d'abord dans un fichier temporaire
        # puis on le bascule en place avec os.replace(), qui est une
        # operation atomique du systeme de fichiers. Ainsi, meme si le
        # script/process est interrompu pendant l'ecriture, self.save_file
        # contient toujours soit l'ancien contenu complet, soit le nouveau
        # complet -- jamais un fichier tronque/corrompu.
        tmp_path = self.save_file + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self.save_file)

    def load(self):
        if not os.path.exists(self.save_file):
            return False
        try:
            with open(self.save_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.players = data["players"]
            self.button = data["button"]
            self.hand_no = data["hand_no"]
            self.small_blind = data["small_blind"]
            self.big_blind = data["big_blind"]
            self.ante = data["ante"]
            self.hands_per_level = data.get("hands_per_level", 10)
            serial_table = data.get("code_table", {})
            self.code_table = {}
            for key, c in serial_table.items():
                r, s = key.split("_")
                self.code_table[(r, s)] = c
            # --- etat de la main en cours (retro-compatible si absent) ---
            self.pot = data.get("pot", 0)
            self.current_bets = data.get("current_bets", {})
            self.board = [self._card_from_list(c) for c in data.get("board", [])]
            self.deck = [self._card_from_list(c) for c in data.get("deck", [])]
            self.hole_cards = {
                n: [self._card_from_list(c) for c in cs]
                for n, cs in data.get("hole_cards", {}).items()
            }
            self.codes = data.get("codes", {})
            self.folded = set(data.get("folded", []))
            self.all_in = set(data.get("all_in", []))
            self.call_only = set(data.get("call_only", []))
            self.street = data.get("street", "preflop")
            self.positions = data.get("positions", {})
            self.last_blocks = data.get("last_blocks", {})
            self.to_act = list(data.get("to_act", []))
            self.seat_order = data.get("seat_order", [])
            self.street_order = data.get("street_order", [])
            self.total_contrib = data.get("total_contrib", {})
            self.min_raise = data.get("min_raise", 0)
            self.history_stack = data.get("history_stack", [])
            self.tournament_log = data.get("tournament_log", [])
            self.tournament_over = data.get("tournament_over", False)
            self.winner = data.get("winner", None)
            self.eliminations = data.get("eliminations", [])
            self.ranking_recorded = data.get("ranking_recorded", False)
            self.hand_complete = data.get("hand_complete", False)
            self.action_log = data.get("action_log", [])
            # retro-compatible : anciennes sauvegardes sans starting_stack /
            # starting_num_players -> on les deduit au mieux de l'etat courant
            # (approximation raisonnable, seulement utilisee tant qu'une
            # nouvelle partie n'a pas ete lancee avec la version a jour).
            self.starting_stack = data.get(
                "starting_stack",
                max((p["stack"] for p in self.players), default=0)
            )
            self.starting_num_players = data.get("starting_num_players", len(self.players))
            self.game_type = data.get("game_type", "tournament")
            self.cash_over = data.get("cash_over", False)
            self.cash_ai_busts = data.get("cash_ai_busts", 0)
            self.cash_new_record = data.get("cash_new_record", False)
            self.sent_log_index = data.get("sent_log_index", {})
            return True
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, KeyError, TypeError, ValueError) as e:
            # Fichier corrompu (arret brutal en cours d'ecriture, disque plein,
            # structure inattendue...) : on ne laisse JAMAIS l'exception
            # remonter et planter le programme. On met le fichier fautif de
            # cote (pour une recuperation manuelle eventuelle), on affiche un
            # message clair, et on repart d'une partie vierge plutot que de
            # crasher.
            backup_path = f"{self.save_file}.corrompu.{int(time.time())}"
            try:
                os.replace(self.save_file, backup_path)
                hint = f"une copie a ete conservee sous : {backup_path}"
            except OSError:
                hint = "impossible de conserver une copie du fichier fautif"
            print(f"\n/!\\ ATTENTION : impossible de charger la sauvegarde '{self.save_file}' ({e}).")
            print(f"    {hint}")
            print("    Une nouvelle partie va etre demarree a partir de zero.\n")
            return False

    # ---------- setup ----------
    def setup_players_web(self, stack, name_pool_raw, seat_controllers, human_names,
                           small_blind=100, big_blind=200, ante=10, hands_per_level=10,
                           shared_name_pool=None, game_type="tournament"):
        """Version non-interactive de setup_players(), utilisee par l'interface web.
        seat_controllers : liste de codes ('0'/'1'/'2'/'3' ou nom libre), un par siege.
        human_names : dict {index_siege: nom} pour les sieges humains.

        shared_name_pool : si fourni, ce pool (liste mutable, deja construite
        et melangee en amont) est utilise et consomme directement au lieu
        d'en reconstruire un nouveau. Indispensable pour un tournoi
        multi-table (voir spawn_extra_tables dans poker_web.py) : sans cela,
        chaque table repiocherait independamment dans la meme liste par
        defaut et on obtiendrait des doublons de noms entre tables.

        game_type : "tournament" (par defaut, comportement historique) ou
        "cash" (nombre de joueurs fixe, blindes fixes, rachat automatique
        des sieges IA elimines - voir _process_cash_rebuys)."""
        self.players = []
        self.game_type = game_type
        if shared_name_pool is not None:
            name_pool = shared_name_pool
        elif name_pool_raw and name_pool_raw.strip():
            name_pool = [x.strip() for x in name_pool_raw.split(",") if x.strip()]
            random.shuffle(name_pool)
        else:
            # noms par defaut : les 15 prioritaires sont places en tete de
            # file, les 184 autres ne servent qu'une fois ceux-ci epuises.
            name_pool = build_default_name_pool()

        for i, code in enumerate(seat_controllers):
            controller = CONTROLLER_SHORTCUTS.get(code.strip(), code.strip())
            is_human = controller.lower() == "humain"
            if is_human:
                name = (human_names.get(i) or f"Joueur{i+1}").strip()
            elif name_pool:
                name = name_pool.pop(0)
            else:
                name = f"{controller}_{i+1}"
            self.players.append({
                "name": name, "controller": controller, "is_human": is_human,
                "stack": stack
            })
        self.small_blind = small_blind
        self.big_blind = big_blind
        self.ante = ante
        self.hands_per_level = hands_per_level
        self.button = 0
        self.hand_no = 0
        self.code_table = generate_code_table()
        self.starting_stack = stack              # FIXE pour toute la partie
        self.starting_num_players = len(self.players)  # FIXE pour toute la partie
        self._reset_hand_and_tournament_state()
        self.save()
        return self.code_table_text()

    def _reset_hand_and_tournament_state(self):
        """Reinitialise tout ce qui concerne une main/partie precedente
        (essentiel si on reconfigure une partie deja jouee, sinon l'ancien
        historique resterait mélangé avec le nouveau). Commun a
        setup_players_web() et setup_players()."""
        self.board = []
        self.deck = []
        self.hole_cards = {}
        self.codes = {}
        self.folded = set()
        self.all_in = set()
        self.call_only = set()
        self.current_bets = {}
        self.pot = 0
        self.street = "preflop"
        self.hand_complete = False
        self.action_log = []
        self.total_contrib = {}
        self.min_raise = 0
        self.history_stack = []
        self.tournament_log = []
        self.tournament_over = False
        self.winner = None
        self.eliminations = []
        self.ranking_recorded = False
        self.positions = {}
        self.last_blocks = {}
        self.to_act = []
        self.seat_order = []
        self.street_order = []
        self.cash_over = False
        self.cash_ai_busts = 0
        self.cash_new_record = False
        self.sent_log_index = {}

    def setup_players(self):
        self.players = []
        n = ask_int("Combien de joueurs a la table ? ")
        stack = ask_int("Tapis de depart (identique pour tous les joueurs) : ")

        print("\nListe des noms disponibles pour les sieges geres par une IA.")
        print(f"Liste par defaut ({len(DEFAULT_NAME_POOL)} noms, les {len(PRIORITY_NAME_POOL)} "
              f"premiers sont utilises en priorite) : {', '.join(DEFAULT_NAME_POOL)}")
        raw = input("Appuyez sur Entree pour la garder, ou saisissez une nouvelle liste (separee par des virgules) : ").strip()
        if raw:
            name_pool = [x.strip() for x in raw.split(",") if x.strip()]
            random.shuffle(name_pool)
        else:
            # noms par defaut : les 15 prioritaires sont places en tete de
            # file, les 184 autres ne servent qu'une fois ceux-ci epuises.
            name_pool = build_default_name_pool()

        for i in range(n):
            print(f"\n-- Joueur {i+1} --")
            raw_controller = input(
                "Gere par : [0]=Humain, [1]=Claude, [2]=Vibe, [3]=ChatGPT "
                "(ou tapez un autre nom directement) : "
            ).strip()
            controller = CONTROLLER_SHORTCUTS.get(raw_controller, raw_controller)
            is_human = controller.lower() == "humain"
            if is_human:
                name = input("Votre nom : ").strip()
            else:
                if name_pool:
                    name = name_pool.pop(0)
                    print(f"Nom pioche automatiquement : {name}")
                else:
                    print("Plus de noms disponibles dans la liste fournie.")
                    name = input("Nom (saisie manuelle) : ").strip()
            self.players.append({
                "name": name, "controller": controller, "is_human": is_human,
                "stack": stack
            })
        self.button = 0
        self.hand_no = 0
        self.code_table = generate_code_table()
        self.starting_stack = stack              # FIXE pour toute la partie
        self.starting_num_players = len(self.players)  # FIXE pour toute la partie
        self._reset_hand_and_tournament_state()
        self.save()
        print("\nTable creee avec succes.")
        self.print_and_save_code_table()

    def code_table_text(self):
        by_suit = {"S": [], "H": [], "D": [], "C": []}
        for (r, s), code in self.code_table.items():
            by_suit[s].append((r, code))
        suit_labels = {"S": "Piques", "H": "Coeurs", "D": "Carreaux", "C": "Trefles"}
        lines = []
        for s in ["S", "H", "D", "C"]:
            items = sorted(by_suit[s], key=lambda x: RANK_VALUE[x[0]], reverse=True)
            line = ", ".join(f"{RANK_NAMES[r]}={code}" for r, code in items)
            lines.append(f"{suit_labels[s]} : {line}")
        return "\n".join(lines)

    def print_and_save_code_table(self):
        print("\n=== TABLE DE CORRESPONDANCE (a communiquer UNE SEULE FOIS a chaque IA) ===")
        print("Copiez ce bloc integralement dans le tout premier message envoye a")
        print("chaque IA geant des bots, exactement comme votre table manuelle initiale.")
        print("Elle NE changera plus ensuite pour toute la duree du tournoi.\n")
        text = self.code_table_text()
        print(text)
        os.makedirs("blocs_a_copier", exist_ok=True)
        path = os.path.join("blocs_a_copier", "table_correspondance.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"\n(Table egalement ecrite dans le fichier : {path})")
        print("(Egalement enregistree dans poker_state.json)")

    # ---------- utilitaires de table ----------
    def active_players(self):
        return [p for p in self.players if p["stack"] > 0]

    def order_from_button(self):
        # rotation a partir du bouton
        idx = self.button % len(self.players)
        rotated = self.players[idx:] + self.players[:idx]
        return [p["name"] for p in rotated if p["stack"] > 0]

    def maybe_increase_blinds(self):
        if self.game_type == "cash":
            return  # blindes fixes en cash game, par definition
        hpl = max(1, self.hands_per_level)
        if self.hand_no > 0 and self.hand_no % hpl == 0:
            self.small_blind *= 2
            self.big_blind *= 2
            self.ante = max(10, self.ante * 2)
            print(f"\n*** Augmentation des blinds : {self.small_blind}/{self.big_blind}, ante {self.ante} ***")

    def hands_until_next_level(self):
        """Renvoie le nombre de mains restantes avant la prochaine hausse
        automatique des blindes/antes (voir maybe_increase_blinds), ou None
        en cash game (blindes fixes, la notion n'a pas de sens).

        maybe_increase_blinds() est appelee juste apres l'incrementation de
        self.hand_no, au tout debut de new_hand() : au moment ou ce numero
        de main est atteint, la hausse a donc DEJA ete appliquee (si
        hand_no % hpl == 0). Pour l'affichage ("plus que N main(s) avant la
        prochaine hausse"), on se place donc juste apres la main courante :
        - si hand_no % hpl != 0 : il reste (hpl - hand_no % hpl) mains.
        - si hand_no % hpl == 0 (hausse qui vient de s'appliquer, ou aucune
          main jouee) : la prochaine hausse est dans un cycle complet, hpl
          mains plus tard.
        """
        if self.game_type == "cash":
            return None
        hpl = max(1, self.hands_per_level)
        remainder = self.hand_no % hpl
        return hpl - remainder if remainder != 0 else hpl

    # ---------- nouvelle main ----------
    def new_hand(self):
        if self.tournament_over:
            print(f"Le tournoi est termine ! Vainqueur : {self.winner}. "
                  "Impossible de lancer une nouvelle main.")
            return
        if self.cash_over:
            print(f"Cash game termine : vous avez ete elimine apres avoir vu "
                  f"{self.cash_ai_busts} IA se faire eliminer. Impossible de lancer une nouvelle main.")
            return
        if self.hand_no > 0:
            self.advance_button_auto()
        self.hand_no += 1
        self.maybe_increase_blinds()
        self.deck = make_deck()
        random.shuffle(self.deck)
        self.board = []
        self.hole_cards = {}
        self.codes = {}
        self.folded = set()
        self.all_in = set()
        self.call_only = set()
        self.current_bets = {}
        self.pot = 0
        self.street = "preflop"
        self.hand_complete = False
        self.action_log = []
        self.sent_log_index = {}
        self.total_contrib = {}
        self.history_stack = []
        self.min_raise = self.big_blind

        order = self.order_from_button()
        if len(order) < 2:
            print("Pas assez de joueurs actifs pour continuer le tournoi !")
            return

        def contribute(name, amount):
            self.total_contrib[name] = self.total_contrib.get(name, 0) + amount

        # antes
        for name in order:
            p = self.find(name)
            a = min(self.ante, p["stack"])
            p["stack"] -= a
            self.pot += a
            contribute(name, a)
            if p["stack"] == 0:
                self.all_in.add(name)

        # positions
        n = len(order)
        sb_name = order[1 % n] if n > 2 else order[0]
        bb_name = order[2 % n] if n > 2 else order[1]

        # blinds (heads-up: bouton = SB)
        if n == 2:
            sb_name, bb_name = order[0], order[1]

        sb_p = self.find(sb_name)
        bb_p = self.find(bb_name)
        sb_amt = min(self.small_blind, sb_p["stack"])
        bb_amt = min(self.big_blind, bb_p["stack"])
        sb_p["stack"] -= sb_amt
        bb_p["stack"] -= bb_amt
        self.pot += sb_amt + bb_amt
        contribute(sb_name, sb_amt)
        contribute(bb_name, bb_amt)
        self.current_bets = {sb_name: sb_amt, bb_name: bb_amt}
        if sb_p["stack"] == 0:
            self.all_in.add(sb_name)
        if bb_p["stack"] == 0:
            self.all_in.add(bb_name)

        # distribution (les cartes sont reellement melangees a chaque main ;
        # seul le CODE associe a chaque carte reste fixe pour toute la partie)
        if not self.code_table:
            print("ERREUR : aucune table de correspondance chargee. "
                  "Relancez '1. Nouvelle partie' pour en generer une.")
            return
        for name in order:
            c1, c2 = self.deck.pop(), self.deck.pop()
            self.hole_cards[name] = [c1, c2]
            self.codes[name] = [self.code_table[c1], self.code_table[c2]]

        # tour de mise pre-flop : ordre reel de parole (file ordonnee, pas un simple ensemble)
        self.seat_order = order
        talk_order = order[3:] + order[:3] if n > 3 else order
        self.street_order = talk_order
        preflop_candidates = [x for x in talk_order if x not in self.all_in]
        self.to_act = preflop_candidates if len(preflop_candidates) >= 2 else []

        self.action_log.append(
            f"Main #{self.hand_no} | Blinds {self.small_blind}/{self.big_blind} | Ante {self.ante}"
        )

        self.save()

        # calcul des positions (BTN, SB, BB, UTG, ...) pour les joueurs actifs
        labels = position_labels_for(n)
        self.positions = {name: labels[i] for i, name in enumerate(order)}
        self.save()

        print(f"\n=========== MAIN #{self.hand_no} ===========")
        print(f"Blinds : {self.small_blind}/{self.big_blind}  Ante : {self.ante}")
        print(f"Ordre de parole pre-flop (du 1er a agir au dernier) :")
        print(" -> ".join(f"{name} ({self.positions[name]})" for name in talk_order))
        print(f"Pot de depart (antes+blinds) : {self.pot}")

        # --- Etat public de la table (visible par tous, positions + tapis) ---
        print("\n--- ETAT DE LA TABLE (info publique) ---")
        for name in order:
            p = self.find(name)
            marker = " <- VOUS" if p["is_human"] else ""
            print(f"{self.positions[name]:8s} | {name:20s} | tapis : {p['stack']:>7d}{marker}")
        eliminated = [p for p in self.players if p["stack"] <= 0]
        if eliminated:
            print("Joueurs elimines : " + ", ".join(p["name"] for p in eliminated))

        print("\n--- CARTES ---")
        for name in order:
            p = self.find(name)
            if p["is_human"]:
                c1, c2 = self.hole_cards[name]
                print(f"{name} (VOUS, {self.positions[name]}) : {card_label(c1)} {card_label(c2)}  [en clair]")

        # --- Blocs groupes par IA, prets a copier-coller ---
        controllers = {}
        for name in order:
            p = self.find(name)
            if not p["is_human"]:
                controllers.setdefault(p["controller"], []).append(name)

        self.last_blocks = {}
        for controller, names in controllers.items():
            lines = []
            # Joueur(s) de ce controleur qui viennent d'arriver a cette table
            # suite a la fermeture d'une table annexe (equilibrage du
            # tournoi multi-table, voir _balance_satellite_tables). On
            # consomme le marqueur ici : il ne sera donc affiche qu'UNE
            # SEULE fois, sur le tout premier bloc envoye a cette IA apres
            # l'arrivee (les mains suivantes redeviennent des blocs normaux).
            arrived = [pname for pname in names if self.find(pname).get("just_arrived")]
            if arrived:
                # L'explication varie selon la raison de l'arrivee (fusion
                # d'une table annexe en tournoi multi-table, ou rachat
                # automatique apres elimination en cash game) - meme
                # mecanique de fond (just_arrived), texte adapte au contexte.
                reasons = {self.find(pname).get("arrival_reason") for pname in arrived}
                if reasons == {"cash_rebuy"}:
                    contexte = (
                        f"{'a' if len(arrived) == 1 else 'ont'} ete elimine{'s' if len(arrived) > 1 else ''} "
                        f"puis immediatement rachete{'s' if len(arrived) > 1 else ''} (cash game : le nombre de "
                        f"joueurs a la table reste toujours le meme, avec un tapis egal a la moyenne actuelle "
                        f"de la table)"
                    )
                else:
                    contexte = (
                        "vien" + ("nent" if len(arrived) > 1 else "t") + " de rejoindre cette table "
                        "(une table annexe du tournoi multi-table vient de fermer et ses joueurs "
                        "restants ont ete redistribues)"
                    )
                lines.append(
                    f"*** NOUVEAU JOUEUR SOUS VOTRE CONTROLE : {', '.join(arrived)} {contexte}. "
                    f"Vous devez desormais jouer son/leur role EN PLUS de ceux que vous controliez "
                    f"deja a cette table, s'il y en a. Comme pour vos autres joueurs, ses/leurs "
                    f"cartes chiffrees vous sont revelees ci-dessous : ne les oubliez pas dans vos "
                    f"actions a venir. ***"
                )
            if len(names) > 1:
                lines.append(f"*** RAPPEL : vous controlez {len(names)} joueurs a cette table : "
                              f"{', '.join(names)}. N'oubliez aucun d'entre eux ! ***")
            lines.append(f"Main #{self.hand_no} | Blinds {self.small_blind}/{self.big_blind} | Ante {self.ante} | Pot de depart : {self.pot}")
            lines.append("Etat de la table (positions et tapis) :")
            for pname in order:
                pl = self.find(pname)
                tag = " (vous)" if pname in names else ""
                lines.append(f"  - {pname} : {self.positions[pname]}, tapis {pl['stack']}{tag}")
            lines.append("Vos cartes chiffrees :")
            for pname in names:
                c1code, c2code = self.codes[pname]
                lines.append(f"  - {pname} ({self.positions[pname]}) : {c1code} et {c2code}")
            lines.append(f"L'action commence pre-flop. Premier a parler : {talk_order[0]} ({self.positions[talk_order[0]]}).")
            self.last_blocks[controller] = "\n".join(lines)
            # Ce controleur vient de recevoir, via ce bloc initial, tout ce
            # qui est dans self.action_log jusqu'ici (juste l'en-tete "Main
            # #..." a ce stade) : les prochains messages qu'on lui enverra
            # sur cette main ne devront donc contenir que ce qui vient
            # APRES (voir hand_delta_text).
            self.sent_log_index[controller] = len(self.action_log)
            for pname in arrived:
                self.find(pname).pop("just_arrived", None)
                self.find(pname).pop("arrival_reason", None)

        self.write_blocks_to_files()
        self.save()
        print("\n--- BLOCS PRETS A COPIER ---")
        print("Chaque bloc a ete ecrit dans un fichier .txt separe (voir liste ci-dessous).")
        print("Ouvrez le fichier voulu (gestionnaire de fichiers / appli texte),")
        print("faites 'Tout selectionner' + 'Copier', puis collez dans le chat de l'IA.")
        for controller in self.last_blocks:
            print(f"  -> {self.block_filepath(controller)}")
        print("(Option '8' du menu pour re-afficher/re-ecrire ces blocs a tout moment.)")

        # Cas extreme : si des le depart plus aucune mise n'est possible
        # (2+ joueurs deja tapis sur les blinds/antes), on enchaine direct.
        if not self.to_act:
            self.check_auto_progress()

    def block_filepath(self, controller):
        safe = "".join(c if c.isalnum() else "_" for c in controller)
        folder = "blocs_a_copier"
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, f"bloc_{safe}.txt")

    def write_blocks_to_files(self):
        for controller, text in self.last_blocks.items():
            path = self.block_filepath(controller)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)

    def show_block(self):
        if not getattr(self, "last_blocks", None):
            print("Aucun bloc disponible. Distribuez d'abord une main (option 2).")
            return
        print("\nIA disponibles : " + ", ".join(self.last_blocks.keys()))
        controller = input("Pour quelle IA afficher/re-ecrire le bloc ? ").strip()
        if controller not in self.last_blocks:
            print("IA inconnue.")
            return
        print("\n" + "=" * 55)
        print(self.last_blocks[controller])
        print("=" * 55)
        path = self.block_filepath(controller)
        print(f"(Egalement re-ecrit dans le fichier : {path})")

    def snapshot_state(self):
        """Capture l'etat mutable de la main en cours, pour pouvoir revenir en
        arriere avec 'annuler la derniere action'."""
        return {
            "pot": self.pot,
            "current_bets": dict(self.current_bets),
            "board": [self._card_to_list(c) for c in self.board],
            "deck": [self._card_to_list(c) for c in self.deck],
            "folded": list(self.folded),
            "all_in": list(self.all_in),
            "call_only": list(self.call_only),
            "to_act": list(self.to_act),
            "street": self.street,
            "street_order": list(self.street_order),
            "action_log": list(self.action_log),
            "total_contrib": dict(self.total_contrib),
            "min_raise": self.min_raise,
            "hand_complete": self.hand_complete,
            "stacks": {p["name"]: p["stack"] for p in self.players},
        }

    def undo_last_action(self):
        """Annule la toute derniere action enregistree (et toute progression
        automatique qu'elle avait declenchee : rue suivante, abattage...)."""
        if not self.history_stack:
            print("Rien a annuler.")
            return
        snap = self.history_stack.pop()
        self.pot = snap["pot"]
        self.current_bets = snap["current_bets"]
        self.board = [self._card_from_list(c) for c in snap["board"]]
        self.deck = [self._card_from_list(c) for c in snap["deck"]]
        self.folded = set(snap["folded"])
        self.all_in = set(snap["all_in"])
        self.call_only = set(snap.get("call_only", []))
        self.to_act = snap["to_act"]
        self.street = snap["street"]
        self.street_order = snap["street_order"]
        self.action_log = snap["action_log"]
        self.total_contrib = snap["total_contrib"]
        self.min_raise = snap["min_raise"]
        self.hand_complete = snap["hand_complete"]
        for pname, stack in snap["stacks"].items():
            p = self.find(pname)
            if p:
                p["stack"] = stack
        self.save()
        print("Derniere action annulee. Etat revenu en arriere.")

    def find(self, name):
        for p in self.players:
            if p["name"] == name:
                return p
        return None

    def active_in_hand(self):
        """Joueurs encore en lice (non couches) dans la main en cours."""
        return [n for n in self.hole_cards if n not in self.folded]

    def reopen_action(self, raiser_name):
        """Reconstruit la file d'attente ordonnee apres une relance : tous les
        autres joueurs encore en lice et non all-in, dans l'ordre reel de la
        table en partant juste apres le relanceur."""
        order = self.street_order or self.seat_order
        if raiser_name in order:
            idx = order.index(raiser_name)
            rotated = order[idx + 1:] + order[:idx + 1]
        else:
            rotated = order
        active = self.active_in_hand()
        return [n for n in rotated if n != raiser_name and n in active and n not in self.all_in]

    # ---------- actions ----------
    def apply_action(self, name, action, amount=None):
        """Applique une action deja parsee. action in {fold,check,call,raise,allin}.
        Verifie que c'est reellement le tour du joueur (file ordonnee) et
        declenche automatiquement la progression quand le tour est termine."""
        if self.hand_complete:
            print("La main est terminee. Lancez une nouvelle main avant d'enregistrer une action.")
            return
        p = self.find(name)
        if not p:
            print(f"Joueur inconnu, ligne ignoree : {name}")
            return
        if name in self.folded:
            print(f"{name} a deja fold cette main, ligne ignoree.")
            return
        if name in self.all_in:
            print(f"{name} est deja all-in, aucune action necessaire, ligne ignoree.")
            return
        if not self.to_act or self.to_act[0] != name:
            prochain = self.to_act[0] if self.to_act else "personne (tour deja termine)"
            print(f"ERREUR : ce n'est pas le tour de {name}. Prochain a agir : {prochain}. Action ignoree.")
            return

        # validation du montant minimum de relance (regle standard du poker :
        # une relance doit au moins egaler la taille de la derniere mise/relance)
        if action in ("raise", "bet"):
            if name in self.call_only:
                print(f"ERREUR : {name} ne peut plus relancer sur cette rue (un adversaire a fait "
                      f"tapis pour une relance incomplete juste avant) : seul un Call ou un Fold "
                      f"est autorise. Action ignoree.")
                return
            if amount is None:
                amount = ask_int(f"Montant total de la mise pour {name} (pas le delta) : ")
            prev_high = max(self.current_bets.values(), default=0)
            increment = amount - prev_high
            would_be_all_in = (amount - self.current_bets.get(name, 0)) >= p["stack"]
            if increment < self.min_raise and not would_be_all_in:
                print(f"ERREUR : relance trop faible pour {name}. Minimum : {prev_high + self.min_raise} "
                      f"au total (incrément d'au moins {self.min_raise}). Action ignoree.")
                return

        # validation de l'all-in tente comme une relance : meme restriction que
        # ci-dessus si {name} avait deja perdu son droit de re-relancer.
        if action == "allin" and name in self.call_only:
            prospective_total = self.current_bets.get(name, 0) + p["stack"]
            if prospective_total > max(self.current_bets.values(), default=0):
                print(f"ERREUR : {name} ne peut plus relancer sur cette rue (un adversaire a fait "
                      f"tapis pour une relance incomplete juste avant) : seul un Call (y compris "
                      f"un tapis pour le complement exact) ou un Fold est autorise. Action ignoree.")
                return

        # validation du check : impossible s'il reste une mise a suivre
        if action == "check":
            owed = max(self.current_bets.values(), default=0) - self.current_bets.get(name, 0)
            if owed > 0:
                print(f"ERREUR : {name} ne peut pas checker, il doit encore suivre {owed} jetons "
                      f"(Call ou Raise necessaire, ou Fold). Action ignoree.")
                return

        # snapshot AVANT modification, pour permettre l'annulation
        self.history_stack.append(self.snapshot_state())
        self.history_stack = self.history_stack[-10:]  # limite la taille de l'historique

        def contribute(pname, amt):
            self.total_contrib[pname] = self.total_contrib.get(pname, 0) + amt

        msg = None
        if action == "fold":
            self.folded.add(name)
            self.to_act.pop(0)
            msg = f"{name} : Fold"
        elif action == "check":
            self.to_act.pop(0)
            msg = f"{name} : Check"
        elif action == "call":
            to_call = max(self.current_bets.values(), default=0) - self.current_bets.get(name, 0)
            to_call = min(to_call, p["stack"])
            p["stack"] -= to_call
            self.pot += to_call
            contribute(name, to_call)
            self.current_bets[name] = self.current_bets.get(name, 0) + to_call
            if p["stack"] == 0:
                self.all_in.add(name)
            self.to_act.pop(0)
            msg = f"{name} : Call {to_call}"
            print(f"{msg} (tapis restant : {p['stack']})")
        elif action in ("raise", "bet"):
            prev_high = max(self.current_bets.values(), default=0)
            requested_total = amount
            delta = amount - self.current_bets.get(name, 0)
            capped = delta > p["stack"]
            delta = min(delta, p["stack"])
            increment = amount - prev_high
            p["stack"] -= delta
            self.pot += delta
            contribute(name, delta)
            self.current_bets[name] = self.current_bets.get(name, 0) + delta
            if p["stack"] == 0:
                self.all_in.add(name)
            full_raise = increment >= self.min_raise
            if full_raise:
                self.min_raise = increment  # relance complete : devient la nouvelle taille minimum
            # une relance rouvre l'action pour tous les autres joueurs encore en lice
            new_queue = self.reopen_action(name)
            if full_raise:
                # relance complete : leve toute restriction "suivre seulement"
                # laissee par une eventuelle relance courte anterieure sur
                # cette rue, puisque l'action est desormais pleinement rouverte.
                self.call_only.clear()
            else:
                # relance incomplete (uniquement possible ici parce que c'est
                # un tapis insuffisant pour couvrir une relance complete,
                # cf. would_be_all_in plus haut) : au poker officiel, cela ne
                # rouvre PAS le droit de re-relancer pour les joueurs qui
                # avaient deja egalise prev_high avant ce coup -- ils ne
                # pourront plus que suivre le complement ou se coucher. Les
                # joueurs qui n'avaient pas encore agi a ce niveau de mise
                # gardent leur option normale (ce n'est que leur tour normal).
                self.call_only.update(
                    n for n in new_queue if self.current_bets.get(n, 0) >= prev_high
                )
            self.to_act = new_queue
            msg = f"{name} : Raise a {self.current_bets[name]}"
            if capped:
                print(f"NOTE : montant annonce ({requested_total}) superieur au tapis disponible, "
                      f"ramene automatiquement a {self.current_bets[name]} (tapis complet).")
                self.action_log.append(
                    f"(Note : {name} avait annonce {requested_total}, plafonne a {self.current_bets[name]} par manque de tapis)"
                )
            print(f"{msg} (tapis restant : {p['stack']})")
        elif action == "allin":
            prev_high = max(self.current_bets.values(), default=0)
            delta = p["stack"]
            new_total = self.current_bets.get(name, 0) + delta
            increment = new_total - prev_high
            p["stack"] = 0
            self.pot += delta
            contribute(name, delta)
            self.current_bets[name] = new_total
            self.all_in.add(name)
            if new_total > prev_high:
                # Le tapis depasse la mise en cours : c'est une vraie relance
                # (complete ou non), on rouvre donc l'action pour les autres
                # joueurs encore en lice.
                full_raise = increment >= self.min_raise
                if full_raise:
                    self.min_raise = increment
                new_queue = self.reopen_action(name)
                if full_raise:
                    self.call_only.clear()
                else:
                    # Relance courte (all-in en dessous du minimum de relance) :
                    # meme regle que dans la branche raise/bet -- ne rouvre le
                    # droit de re-relancer qu'aux joueurs pas encore agi a ce
                    # niveau de mise ; les autres ne pourront que suivre/se coucher.
                    self.call_only.update(
                        n for n in new_queue if self.current_bets.get(n, 0) >= prev_high
                    )
                self.to_act = new_queue
                msg = f"{name} : All-in a {self.current_bets[name]}"
            else:
                # Le tapis est <= a la mise en cours : ce n'est PAS une
                # relance, seulement un call (eventuellement partiel/short).
                # Il ne doit surtout pas rouvrir l'action pour les joueurs
                # qui avaient deja suivi/agi a ce niveau de mise.
                self.to_act.pop(0)
                msg = f"{name} : All-in {new_total} (suit la mise, ne relance pas)"
            print(msg)
        else:
            print(f"Action non reconnue pour {name} : {action}")
            self.history_stack.pop()  # annule le snapshot inutile
            return

        if action in ("fold", "check"):
            print(msg)
        self.action_log.append(msg)
        self.save()
        self.check_auto_progress()

    def check_auto_progress(self):
        """Verifie si le tour de mise (ou la main) est termine, et enchaine
        automatiquement (rue suivante / abattage) si c'est le cas."""
        active = self.active_in_hand()
        if len(active) <= 1:
            print("\n*** Un seul joueur reste en lice : distribution automatique du pot. ***")
            self.showdown()
            return True
        if not self.to_act:
            if self.street == "river":
                print("\n*** Tour de mise termine sur la river. Passage automatique a l'abattage. ***")
                self.showdown()
            else:
                print(f"\n*** Tour de mise termine ({self.street}). Passage automatique a la rue suivante. ***")
                self.next_street()
                return self.check_auto_progress()
            return True
        return False

    def record_action(self):
        name = input("Nom du joueur : ").strip()
        raw = input("Action (fold/check/call/raise/allin) : ").strip().lower()
        amount = None
        if raw in ("raise", "bet", "raise a", "relance"):
            amount = ask_int("Montant total de la mise (pas juste le delta) : ")
            action = "raise"
        elif raw.startswith("raise") or raw.startswith("bet") or raw.startswith("relance"):
            nums = re.findall(r"\d+", raw)
            amount = int(nums[-1]) if nums else ask_int("Montant total de la mise : ")
            action = "raise"
        elif raw.startswith("fold"):
            action = "fold"
        elif raw.startswith("check"):
            action = "check"
        elif raw.startswith("call"):
            action = "call"
        elif raw.startswith("all"):
            action = "allin"
        else:
            print("Action non reconnue.")
            return
        self.apply_action(name, action, amount)

    def parse_action_line(self, line):
        if ":" not in line:
            return None
        name_part, action_part = line.split(":", 1)
        name = name_part.strip().lstrip("-").strip()
        action_part = action_part.strip()
        lower = action_part.lower()
        nums = re.findall(r"\d+", action_part)
        amount = int(nums[-1]) if nums else None
        if lower.startswith("fold"):
            return name, "fold", None
        if lower.startswith("check"):
            return name, "check", None
        if lower.startswith("call"):
            return name, "call", None
        if lower.startswith("raise") or lower.startswith("relance") or lower.startswith("bet") or lower.startswith("mise"):
            return name, "raise", amount
        if lower.startswith("all"):
            return name, "allin", None
        return None

    def record_actions_block(self):
        print("Collez le bloc d'actions (une ligne 'Nom : Action' par joueur).")
        print("Les lignes sans ':' (titres, en-tetes) sont ignorees automatiquement.")
        print("Terminez en entrant une ligne vide.")
        lines = []
        while True:
            line = input()
            if line.strip() == "":
                break
            lines.append(line)
        for line in lines:
            parsed = self.parse_action_line(line)
            if parsed is None:
                continue  # probablement une ligne de titre, ignoree silencieusement
            name, action, amount = parsed
            self.apply_action(name, action, amount)
        self.save()
        print("\nBloc d'actions applique.")

    def show_my_cards(self):
        humans = [p for p in self.players if p["is_human"]]
        if not humans:
            print("Aucun joueur humain configure.")
            return
        for p in humans:
            name = p["name"]
            if name in self.hole_cards:
                c1, c2 = self.hole_cards[name]
                pos = self.positions.get(name, "?")
                print(f"{name} (VOUS, {pos}) : {card_label(c1)} {card_label(c2)}")
            else:
                print(f"{name} : pas de main en cours (distribuez d'abord une main).")

    # ---------- rues ----------
    def next_street(self):
        self.current_bets = {}
        self.min_raise = self.big_blind
        self.call_only = set()  # nouvelle rue = nouveau tour de mise, restriction levee
        if self.street == "preflop":
            self.deck.pop()  # brulage
            self.board = [self.deck.pop() for _ in range(3)]
            self.street = "flop"
        elif self.street == "flop":
            self.deck.pop()
            self.board.append(self.deck.pop())
            self.street = "turn"
        elif self.street == "turn":
            self.deck.pop()
            self.board.append(self.deck.pop())
            self.street = "river"
        else:
            print("La river a deja ete revelee. Passez au showdown.")
            return
        # Avant d'afficher la nouvelle rue, on liste les joueurs encore en jeu
        # (non couches) sur la rue qui vient de se terminer : ainsi les autres
        # IA n'ont pas besoin de recompter les "fold" dans tout l'historique
        # pour savoir qui reste en lice au moment ou la nouvelle carte tombe.
        still_in = self.active_in_hand()
        self.action_log.append("Joueurs encore en jeu : " + ", ".join(still_in))
        print("Joueurs encore en jeu : " + ", ".join(still_in))
        labels = " - ".join(card_label(c) for c in self.board)
        header = f"--- {self.street.upper()} : {labels} (pot : {self.pot}) ---"
        print(f"\n{header}")
        self.action_log.append(header)
        # nouveau tour de mise : ordre reel de parole, en partant juste apres
        # le bouton (regle standard du poker pour les rues post-flop)
        postflop_order = self.seat_order[1:] + self.seat_order[:1] if self.seat_order else []
        active = self.active_in_hand()
        self.street_order = [n for n in postflop_order if n in active]
        candidates = [n for n in self.street_order if n not in self.all_in]
        # Si au plus un seul joueur peut encore agir (les autres etant tous
        # couches ou deja tapis), plus aucune mise n'est reellement possible :
        # on ne bloque pas sur une action qui n'a plus de sens (rien a suivre,
        # personne en face pour repondre a une eventuelle mise).
        self.to_act = candidates if len(candidates) >= 2 else []
        self.save()

    # ---------- showdown ----------
    def compute_side_pots(self):
        """Decoupe le pot total en 'couches' en fonction de TOUS les niveaux
        de mise distincts (algorithme standard des side-pots), pas seulement
        des joueurs reellement all-in : sinon l'argent mis par un joueur
        couche AVANT le niveau all-in (ex: sa blinde/ante) se retrouvait a
        tort exclu du calcul du pot principal et forme un side-pot fictif,
        alors qu'il n'existe aucune vraie disparite de tapis a ce niveau.
        Les couches consecutives qui ont exactement les memes joueurs
        eligibles sont ensuite fusionnees (ce ne sont pas de vrais side-pots
        distincts, juste des paliers de calcul intermediaires). Se reduit
        naturellement a un seul pot si personne n'est all-in sur cette main."""
        contribs = {n: amt for n, amt in self.total_contrib.items() if amt > 0}
        if not contribs:
            return []
        levels = sorted(set(contribs.values()))
        pots = []
        prev = 0
        for level in levels:
            contributors_at_level = [n for n, c in contribs.items() if c >= level]
            layer_amount = (level - prev) * len(contributors_at_level)
            if layer_amount > 0:
                eligible = [n for n in contributors_at_level if n not in self.folded]
                pots.append({"amount": layer_amount, "eligible": eligible})
            prev = level

        # fusionne les paliers consecutifs ayant exactement les memes joueurs
        # eligibles : il n'y a alors aucune vraie disparite de tapis entre eux,
        # ca reste un seul et meme pot du point de vue de la distribution.
        merged = []
        for pot in pots:
            if merged and merged[-1]["eligible"] == pot["eligible"]:
                merged[-1]["amount"] += pot["amount"]
            else:
                merged.append(dict(pot))
        return merged

    def showdown(self):
        if self.hand_complete:
            print("Cette main a deja ete distribuee.")
            return
        contenders = [n for n in self.hole_cards if n not in self.folded]

        pots = self.compute_side_pots()
        if not pots:
            pots = [{"amount": self.pot, "eligible": contenders}]

        # evalue les mains une seule fois pour tous les contenders
        scores = {}
        if len(contenders) > 1:
            for name in contenders:
                seven = self.hole_cards[name] + self.board
                score = best_hand(seven)
                scores[name] = score
                cards_str = " ".join(card_label(c) for c in self.hole_cards[name])
                line = f"{name} : {cards_str} -> {describe(score)}"
                print(line)
                self.action_log.append(line)

        multi_pots = len(pots) > 1
        for i, pot in enumerate(pots):
            eligible = pot["eligible"]
            amount = pot["amount"]
            pot_label = f"Side-pot #{i}" if (multi_pots and i > 0) else ("Pot principal" if multi_pots else "Pot")
            if not eligible:
                continue  # personne d'eligible (tous couches sur cette couche) : cas theorique
            if len(eligible) == 1:
                winner = eligible[0]
                self.find(winner)["stack"] += amount
                result_line = f">>> {pot_label} ({amount}) : {winner} remporte (seul eligible)."
                print(result_line)
                self.action_log.append(result_line)
                continue
            best_score = max(scores[n] for n in eligible)
            winners = [n for n in eligible if scores[n] == best_score]
            share = amount // len(winners)
            remainder = amount - share * len(winners)
            for j, w in enumerate(winners):
                self.find(w)["stack"] += share + (remainder if j == 0 else 0)
            if len(winners) == 1:
                result_line = f">>> {pot_label} ({amount}) : {winners[0]} remporte avec {describe(best_score)} !"
            else:
                result_line = f">>> {pot_label} ({amount}) partage entre : {', '.join(winners)} ({describe(best_score)})"
            print(result_line)
            self.action_log.append(result_line)

        self.hand_complete = True
        self.to_act = []

        # archive la main dans l'historique complet du tournoi
        if self.action_log:
            self.tournament_log.append("\n".join(self.action_log))

        self.save()
        self.print_stacks()

        # suivi des eliminations (pour le classement inter-tournois) et
        # detection de fin de partie - le comportement differe totalement
        # entre un tournoi (elimination definitive) et un cash game (rachat
        # automatique des sieges IA, nombre de joueurs toujours fixe).
        if self.game_type == "cash":
            self._process_cash_rebuys()
        else:
            already_out = {e["name"] for e in self.eliminations}
            for pl in self.players:
                if pl["stack"] <= 0 and pl["name"] not in already_out:
                    self.eliminations.append({"name": pl["name"], "hand_no": self.hand_no})

            # detection de fin de tournoi
            remaining = [pl for pl in self.players if pl["stack"] > 0]
            if len(remaining) == 1:
                self.tournament_over = True
                self.winner = remaining[0]["name"]
                banner = f"\n*** TOURNOI TERMINE ! Vainqueur : {self.winner} avec {remaining[0]['stack']} jetons. ***"
                print(banner)
                self.save()
                self.record_tournament_ranking()

    def _process_cash_rebuys(self):
        """Mode cash game uniquement : quand un siege tombe a 0 jeton, il
        est immediatement rachete pour garder un nombre de joueurs FIXE a
        la table :
          - Siege IA : rachete automatiquement sous un nouveau nom (la
            MEME IA en prend le controle), avec un tapis egal a la
            moyenne des tapis des joueurs encore solvables. Le marqueur
            just_arrived (mecanique deja utilisee pour le multi-table)
            previent l'IA, dans le tout prochain bloc de main, qu'elle
            controle desormais ce nouveau joueur. Le compteur
            cash_ai_busts (le "record a battre" affiche au joueur humain)
            est incremente a chaque rachat.
          - Siege humain : pas de rachat automatique - la partie s'arrete
            pour le joueur humain (cash_over=True), et si le nombre d'IA
            eliminees pendant cette session bat le record persistant
            (get_cash_best_record), il est mis a jour."""
        busted = [pl for pl in self.players if pl["stack"] <= 0]
        if not busted:
            return

        ai_busted = [pl for pl in busted if not pl.get("is_human")]
        human_busted = any(pl.get("is_human") for pl in busted)

        if ai_busted:
            survivors = [pl["stack"] for pl in self.players if pl["stack"] > 0]
            avg_stack = round(sum(survivors) / len(survivors)) if survivors else self.starting_stack
            used_names = {pl["name"] for pl in self.players}
            pool = build_default_name_pool()
            for pl in ai_busted:
                new_name = next((n for n in pool if n not in used_names), None)
                if new_name is None:
                    new_name = f"{pl['controller']}_{self.hand_no}_{len(used_names)}"
                used_names.add(new_name)
                old_name = pl["name"]
                pl["name"] = new_name
                pl["stack"] = avg_stack
                pl["just_arrived"] = True
                pl["arrival_reason"] = "cash_rebuy"
                self.cash_ai_busts += 1
                print(f"\n*** {old_name} ({pl['controller']}) est elimine et rachete sous le nom "
                      f"{new_name}, avec {avg_stack} jetons (tapis moyen de la table). ***")

        if human_busted:
            self.cash_over = True
            previous_best = self.get_cash_best_record()
            self.cash_new_record = self.cash_ai_busts > previous_best
            if self.cash_new_record:
                self._save_cash_best_record(self.cash_ai_busts)
            print(f"\n*** Cash game termine : vous etes elimine apres avoir vu {self.cash_ai_busts} "
                  f"IA se faire eliminer-racheter. ***")

        self.save()

    # ---------- synchronisation multi-table (tables annexes IA) ----------
    @staticmethod
    def _random_shares(total, n):
        """Decoupe 'total' en n parts entieres aleatoires (>= 0) dont la
        somme vaut exactement 'total' (aucun jeton cree/perdu). Commun a
        eliminate_random_player() et apply_pot_shuffle()."""
        if n <= 1:
            return [total]
        cuts = sorted(random.randint(0, total) for _ in range(n - 1))
        bounds = [0] + cuts + [total]
        shares = [bounds[i + 1] - bounds[i] for i in range(n)]
        random.shuffle(shares)
        return shares

    def _record_elimination(self, name):
        """Ajoute 'name' a self.eliminations (avec le numero de main courant)
        si ce n'est pas deja fait. Factorise le meme garde-fou repete dans
        eliminate_random_player() et apply_pot_shuffle(). Renvoie True si
        l'elimination vient effectivement d'etre enregistree (False si le
        joueur etait deja marque comme elimine)."""
        already_out = {e["name"] for e in self.eliminations}
        if name in already_out:
            return False
        self.eliminations.append({"name": name, "hand_no": self.hand_no})
        return True

    def eliminate_random_player(self, exempt_humans=True):
        """Elimine au hasard un joueur ENCORE ACTIF (tapis > 0) de cette
        table et reparti aleatoirement son tapis entre les autres joueurs
        encore actifs de la meme table. N'est jamais declenche par le jeu
        normal : sert uniquement a synchroniser une table ANNEXE (IA
        uniquement, sur laquelle aucun coup n'est joue) lorsqu'un joueur
        vient d'etre elimine sur la table ou vous jouez, afin que toutes
        les tables du tournoi multi-table perdent des joueurs au meme
        rythme.

        Si exempt_humans est vrai (par defaut), un siege humain de CETTE
        table ne peut jamais etre tire au sort (une table annexe n'en
        contient normalement aucun, mais la protection ne coute rien).

        Ne fait rien et renvoie None si la table compte deja 1 joueur actif
        ou moins (rien a eliminer)."""
        pool = [p for p in self.players if p["stack"] > 0]
        if exempt_humans:
            pool = [p for p in pool if not p.get("is_human")]
        if len(pool) <= 1:
            return None

        victim = random.choice(pool)
        amount = victim["stack"]
        victim["stack"] = 0

        # Repartition aleatoire du tapis du joueur elimine entre les autres
        # joueurs encore actifs de SA table (parts aleatoires, mais dont la
        # somme vaut exactement le tapis elimine : aucun jeton cree/perdu).
        survivors = [p for p in self.players if p is not victim and p["stack"] > 0]
        if amount > 0 and survivors:
            shares = self._random_shares(amount, len(survivors))
            for p, share in zip(survivors, shares):
                p["stack"] += share

        self._record_elimination(victim["name"])

        # NB : on ne detecte PAS ici la fin de tournoi et on n'appelle PAS
        # record_tournament_ranking(). Cette methode ne synchronise qu'une
        # table ANNEXE d'un tournoi multi-table encore en cours : un passage
        # a 1 seul joueur actif ici n'est qu'un signal local ("plus rien a
        # eliminer sur cette table"), pas une vraie fin de tournoi. La vraie
        # fin de tournoi et l'enregistrement du classement ne doivent etre
        # decides que par la table humaine, via showdown(), une fois tous
        # les joueurs fusionnes dessus (voir _balance_satellite_tables).

        self.save()
        return victim["name"]

    def apply_pot_shuffle(self, amount, exempt_humans=True):
        """Fait circuler aleatoirement, entre les joueurs ENCORE ACTIFS (tapis
        > 0) de cette table, un montant equivalent a 'amount' (typiquement le
        pot final d'une main qui vient d'etre jouee sur la table PRINCIPALE
        d'un tournoi multi-table). Une partie des jetons est d'abord
        prelevee au hasard sur certains de ces joueurs (plafonnee a ce que
        chacun possede reellement, et au total des jetons presents parmi
        eux), puis ce meme montant est integralement redistribue au hasard
        entre les joueurs restants - sans jamais creer ni detruire le
        moindre jeton.

        Sert uniquement a simuler, sur une table ANNEXE (IA uniquement, sur
        laquelle aucune vraie main n'est jamais jouee) d'un tournoi
        multi-table, la circulation de jetons qu'une vraie main provoquerait :
        sans cela, les tapis d'une table annexe ne pourraient QU'augmenter
        (uniquement lors d'une elimination miroir, voir
        eliminate_random_player), ce qui ne ressemble pas a un vrai tournoi ou
        les tapis montent ET descendent a chaque main.

        Exactement comme une vraie main, ce mouvement de pot peut faire
        tomber un ou plusieurs joueurs a 0 jeton : ces eliminations sont
        enregistrees dans self.eliminations, comme le fait deja
        eliminate_random_player (meme remarque : on ne detecte PAS ici de fin
        de tournoi, laissee a la table principale). Renvoie la liste des noms
        nouvellement elimines par cet appel (liste vide si aucun, ou si rien
        n'a pu etre fait : moins de 2 joueurs actifs eligibles, ou montant
        nul/negatif).

        Si exempt_humans est vrai (par defaut), un siege humain de CETTE
        table ne peut jamais etre implique dans le prelevement (une table
        annexe n'en contient normalement aucun, mais la protection ne coute
        rien)."""
        pool = [p for p in self.players if p["stack"] > 0]
        if exempt_humans:
            pool = [p for p in pool if not p.get("is_human")]
        if len(pool) < 2 or amount <= 0:
            return []

        # Le montant preleve ne peut jamais depasser le total des jetons
        # reellement presents parmi ces joueurs : une vraie main ne peut pas
        # faire circuler plus de jetons qu'il n'y en a sur la table.
        total_available = sum(p["stack"] for p in pool)
        amount = min(amount, total_available)
        if amount <= 0:
            return []

        # ---- phase 1 : prelevement aleatoire (qui "perd" quoi) ----
        # Ordre aleatoire, puis a chaque joueur on tire un prelevement entre
        # un minimum (ce qu'il DOIT ceder pour que le reste du montant reste
        # atteignable avec les joueurs suivants) et un maximum (son propre
        # tapis, plafonne au solde restant a collecter) : le total collecte
        # correspond ainsi exactement a 'amount', sans jamais debiter un
        # joueur au-dela de son tapis.
        order = list(pool)
        random.shuffle(order)
        remaining = amount
        debits = {}
        for i, p in enumerate(order):
            stacks_after = sum(pl["stack"] for pl in order[i + 1:])
            low = max(0, remaining - stacks_after)
            high = min(p["stack"], remaining)
            take = random.randint(low, high)
            debits[p["name"]] = take
            remaining -= take
        for p in order:
            p["stack"] -= debits[p["name"]]

        # Joueurs tombes a 0 lors de ce prelevement : eliminations
        # potentielles (sauf s'ils regagnent quelque chose en phase 2, mais
        # ils en sont exclus puisque la liste des beneficiaires ci-dessous
        # est calculee APRES le prelevement).
        newly_busted = [p for p in order if p["stack"] == 0]

        # ---- phase 2 : redistribution aleatoire (qui "gagne" quoi) ----
        survivors = [p for p in self.players if p["stack"] > 0]
        if survivors:
            shares = self._random_shares(amount, len(survivors))
            for p, share in zip(survivors, shares):
                p["stack"] += share

        # ---- enregistrement des eliminations (comme eliminate_random_player) ----
        newly_eliminated = []
        for p in newly_busted:
            if p["stack"] == 0 and self._record_elimination(p["name"]):
                newly_eliminated.append(p["name"])

        self.save()
        return newly_eliminated

    # ---------- classement inter-tournois ----------
    def compute_tournament_ranking(self):
        """Calcule le classement final d'un tournoi termine, par CONTROLEUR
        (une IA ou un humain peut controler plusieurs joueurs : on fait
        alors la moyenne de leurs placements). Retourne un dict
        controleur -> points gagnes (3/2/1/0, seuls les 3 premiers marquent).
        Ne fait rien (dict vide) si le tournoi n'est pas termine."""
        if not self.tournament_over or not self.winner:
            return {}

        # Construit la liste des placements : le vainqueur est 1er, puis les
        # elimines dans l'ordre INVERSE d'elimination (le dernier sorti = 2eme).
        placements = []  # [(nom, rang, hand_no_de_sortie)]
        placements.append((self.winner, 1, self.hand_no))
        for rank, elim in enumerate(reversed(self.eliminations), start=2):
            placements.append((elim["name"], rank, elim["hand_no"]))

        # Regroupe par controleur
        by_controller = {}
        for name, rank, hand_out in placements:
            p = self.find(name)
            if not p:
                continue
            ctrl = p["controller"]
            by_controller.setdefault(ctrl, []).append((rank, hand_out))

        # Moyenne par controleur, puis classement (rang moyen croissant,
        # depart. par la moyenne des mains de sortie decroissante = a survecu
        # plus longtemps en moyenne)
        ctrl_avg = []
        for ctrl, entries in by_controller.items():
            avg_rank = sum(r for r, _ in entries) / len(entries)
            avg_hand_out = sum(h for _, h in entries) / len(entries)
            ctrl_avg.append((ctrl, avg_rank, avg_hand_out))
        ctrl_avg.sort(key=lambda x: (x[1], -x[2]))

        # Bareme lineaire sur TOUS les controleurs : 0 point pour le dernier,
        # 1 pour l'avant-dernier, 2 pour l'avant-avant-dernier, etc. (le
        # vainqueur empoche donc k-1 points, k etant le nombre de controleurs
        # distincts a la table).
        k = len(ctrl_avg)
        result = {}
        for i, (ctrl, avg_rank, avg_hand_out) in enumerate(ctrl_avg):
            result[ctrl] = (k - 1) - i
        return result

    # ---------- classement general (persistant, multi-tournois) ----------
    def _load_leaderboard(self):
        if not os.path.exists(LEADERBOARD_FILE):
            return {}
        try:
            with open(LEADERBOARD_FILE, "r", encoding="utf-8") as f:
                board = json.load(f)
            if not isinstance(board, dict):
                raise ValueError("le contenu du classement n'est pas un objet JSON valide")
            return board
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError) as e:
            # Meme logique de recuperation que pour Game.load() : un
            # classement corrompu ne doit jamais faire planter le
            # programme. On le met de cote et on repart d'un classement
            # vierge (aucune partie en cours n'est affectee, ce fichier est
            # independant de SAVE_FILE).
            backup_path = f"{LEADERBOARD_FILE}.corrompu.{int(time.time())}"
            try:
                os.replace(LEADERBOARD_FILE, backup_path)
                hint = f"une copie a ete conservee sous : {backup_path}"
            except OSError:
                hint = "impossible de conserver une copie du fichier fautif"
            print(f"\n/!\\ ATTENTION : classement general illisible ({LEADERBOARD_FILE}) : {e}.")
            print(f"    {hint}")
            print("    Un classement vierge sera utilise a partir de maintenant.\n")
            return {}

    def _save_leaderboard(self, board):
        """Ecriture atomique : on ecrit d'abord dans un fichier temporaire
        puis on le bascule en place avec os.replace(), qui est une
        operation atomique du systeme de fichiers. Ainsi, meme si le
        processus est interrompu ou si un lecteur (get_leaderboard) tombe
        pile pendant l'ecriture, poker_leaderboard.json contient toujours
        soit l'ancien contenu complet, soit le nouveau complet -- jamais
        un fichier tronque/corrompu."""
        tmp_path = LEADERBOARD_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(board, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, LEADERBOARD_FILE)

    def record_tournament_ranking(self):
        """Ajoute au classement general (fichier PARTAGE entre toutes les
        parties) les points gagnes par chaque controleur a l'issue de ce
        tournoi. Ne fait rien si le tournoi n'est pas termine ou si ce
        tournoi a deja ete comptabilise (self.ranking_recorded).

        La sequence lire -> incrementer -> ecrire est executee sous
        _leaderboard_lock() : sans ca, l'ecriture atomique seule ne suffit
        pas a empecher une perte de points si deux tables terminent en
        meme temps (les deux liraient le meme etat de depart et la
        deuxieme ecriture ecraserait les points ajoutes par la premiere).
        Le verrou serialise ces sequences entre parties/onglets."""
        if self.ranking_recorded:
            return
        points = self.compute_tournament_ranking()
        if not points:
            return
        with _leaderboard_lock():
            board = self._load_leaderboard()
            for ctrl, pts in points.items():
                board[ctrl] = board.get(ctrl, 0) + pts
            self._save_leaderboard(board)
        self.ranking_recorded = True
        self.save()

    def get_leaderboard(self):
        """Retourne le classement general trie par points decroissants :
        liste de tuples (controleur, points)."""
        board = self._load_leaderboard()
        return sorted(board.items(), key=lambda x: -x[1])

    # ---------- record cash game (persistant, multi-parties) ----------
    def get_cash_best_record(self):
        """Renvoie le meilleur score cash game jamais atteint (nombre
        d'IA eliminees-rachetees par le joueur humain avant sa propre
        elimination), tous parties confondues. 0 si aucun record n'a
        encore ete etabli ou si le fichier est illisible/absent."""
        if not os.path.exists(CASH_RECORD_FILE):
            return 0
        try:
            with open(CASH_RECORD_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return int(data.get("best", 0)) if isinstance(data, dict) else 0
        except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError, TypeError):
            return 0

    def _save_cash_best_record(self, value):
        tmp_path = CASH_RECORD_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump({"best": value}, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, CASH_RECORD_FILE)

    # ---------- affichage ----------
    def hand_summary_text(self):
        """Retourne l'historique de la main en cours, au format copiable
        (identique a celui que vous tapez manuellement pour les autres IA).
        Utilise pour le collage manuel (bloc entier, toujours complet) -
        voir hand_delta_text() pour la version allegee utilisee par le
        pilotage automatique via le relais."""
        if not self.action_log:
            return "(aucune action enregistree pour l'instant sur cette main)"
        text = "\n".join(self.action_log)
        if not self.action_log[0].startswith("Main #"):
            text = ("(Note : le debut de cette main a eu lieu avant une mise a jour du "
                     "journal, ces premieres actions ne sont pas listees ci-dessous)\n") + text
        if self.hand_complete:
            text += "\n(Main terminee.)"
        elif self.to_act:
            next_player = self.to_act[0]
            pos = self.positions.get(next_player, "")
            pos_txt = f" ({pos})" if pos else ""
            text += f"\nProchain a parler : {next_player}{pos_txt}."
        return text

    def hand_delta_text(self, controller):
        """Version allegee de hand_summary_text(), pensee pour le
        pilotage automatique via le relais : ne renvoie que les lignes
        du journal de la main en cours qui n'ont PAS ENCORE ete envoyees
        a CE controleur precis (voir sent_log_index), au lieu de repeter
        a chaque tour tout l'historique deja transmis dans les messages
        precedents de la meme conversation - l'IA le retient deja, la
        repetition ne fait que gonfler inutilement chaque echange.

        N'avance PAS le pointeur elle-meme : appeler mark_log_sent()
        seulement apres un envoi reussi, pour pouvoir renvoyer exactement
        le meme contenu en cas de nouvel essai suite a un echec."""
        start = self.sent_log_index.get(controller, 0)
        new_lines = self.action_log[start:]
        text = "\n".join(new_lines) if new_lines else "(Aucune nouvelle action depuis votre dernier message sur cette main.)"
        if self.hand_complete:
            text += "\n(Main terminee.)"
        elif self.to_act:
            next_player = self.to_act[0]
            pos = self.positions.get(next_player, "")
            pos_txt = f" ({pos})" if pos else ""
            text += f"\nProchain a parler : {next_player}{pos_txt}."
        return text

    def mark_log_sent(self, controller):
        """A appeler juste apres qu'un message construit par
        hand_delta_text(controller) a ete envoye AVEC SUCCES : avance le
        pointeur de ce controleur jusqu'au bout du journal actuel, pour
        que son prochain message ne contienne que ce qui viendra APRES."""
        self.sent_log_index[controller] = len(self.action_log)

    def full_tournament_text(self):
        """Retourne l'historique complet du tournoi (toutes les mains deja
        terminees), chacune separee par une ligne vide."""
        if not self.tournament_log:
            return "(aucune main terminee pour l'instant dans ce tournoi)"
        return "\n\n".join(self.tournament_log)

    def print_stacks(self):
        print("\n--- Tapis actuels ---")
        for p in sorted(self.players, key=lambda x: -x["stack"]):
            statut = " (ELIMINE)" if p["stack"] <= 0 else ""
            print(f"{p['name']} ({p['controller']}) : {p['stack']}{statut}")

    def print_state(self):
        print(f"\nMain #{self.hand_no} | Rue : {self.street} | Pot : {self.pot}")
        print(f"Blindes {self.small_blind}/{self.big_blind}, ante {self.ante} "
              f"(prochaine hausse dans {self.hands_until_next_level()} main(s))")
        if self.board:
            print("Board : " + " - ".join(card_label(c) for c in self.board))
        self.print_stacks()

    def advance_button(self):
        n = len(self.players)
        self.button = (self.button + 1) % n
        self.save()

    def advance_button_auto(self):
        """Avance le bouton au prochain joueur encore actif (tapis > 0)."""
        n = len(self.players)
        for _ in range(n):
            self.button = (self.button + 1) % n
            if self.players[self.button]["stack"] > 0:
                break
        self.save()


# ----------------------------------------------------------------------
# Menu principal
# ----------------------------------------------------------------------

def main():
    g = Game()
    if g.load():
        print(f"Partie existante chargee (main #{g.hand_no}).")
    while True:
        print("""
==================== MENU ====================
1. Nouvelle partie (reinitialise tout)
2. Nouvelle main (melange + distribution)
3. Enregistrer une action de mise
4. Passer a la rue suivante (flop/turn/river)
5. Showdown / distribuer le pot
6. Etat de la table
7. Avancer le bouton (fin de main manuelle)
8. Re-afficher / re-ecrire le bloc a copier pour une IA
9. Re-afficher / re-ecrire la table de correspondance
10. Afficher a nouveau mes cartes
11. Enregistrer un BLOC d'actions (coller plusieurs lignes d'un coup)
12. Annuler la derniere action
13. Historique complet du tournoi
0. Quitter
================================================""")
        choice = input("Choix : ").strip()
        if choice == "1":
            g.setup_players()
        elif choice == "2":
            g.new_hand()
        elif choice == "3":
            g.record_action()
        elif choice == "4":
            g.next_street()
        elif choice == "5":
            g.showdown()
        elif choice == "6":
            g.print_state()
        elif choice == "7":
            g.advance_button()
            print("Bouton avance.")
        elif choice == "8":
            g.show_block()
        elif choice == "9":
            if g.code_table:
                g.print_and_save_code_table()
            else:
                print("Aucune table de correspondance chargee. Creez d'abord une partie (option 1).")
        elif choice == "10":
            g.show_my_cards()
        elif choice == "11":
            g.record_actions_block()
        elif choice == "12":
            g.undo_last_action()
        elif choice == "13":
            print("\n" + g.full_tournament_text())
        elif choice == "0":
            g.save()
            print("Partie sauvegardee. A bientot !")
            break
        else:
            print("Choix invalide.")


if __name__ == "__main__":
    main()
