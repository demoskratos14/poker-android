#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - Point d'entree de l'application Android.

Principe : on ne reecrit pas l'interface (poker_web.py fait deja tout
en Flask). On se contente de :
  1. Demarrer le serveur Flask en arriere-plan (thread daemon), sur
     127.0.0.1:5000, des le lancement de l'app.
  2. Remplacer l'ecran de l'application par une VRAIE WebView Android
     (le composant natif android.webkit.WebView, celui utilise par
     Chrome lui-meme) qui affiche cette page locale en plein ecran.
  3. Faire en sorte que le bouton "Retour" physique du telephone
     revienne a la page precedente DANS la table de poker (comme le
     ferait un navigateur), et ferme l'app seulement s'il n'y a plus
     d'historique (comportement Android standard).

Resultat : plus de navigateur externe, plus de barre d'adresse.
L'utilisateur voit uniquement la table de poker, comme une app
normale. Le jeu (poker_engine.py + poker_web.py) tourne entierement
sur l'appareil, sans connexion internet.
"""

import threading

from kivy.app import App
from kivy.clock import Clock
from kivy.uix.label import Label
from kivy.utils import platform

# On importe l'app Flask SANS la lancer : poker_web.py protege son
# app.run() derriere `if __name__ == "__main__":`, donc l'import seul
# ne demarre rien.
from poker_web import app as flask_app

SERVER_URL = "http://127.0.0.1:5000"
_server_started = False


def run_flask():
    """Lance le serveur Flask. A executer uniquement dans un thread
    a part : app.run() est bloquant."""
    flask_app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)


def start_server_once():
    global _server_started
    if _server_started:
        return
    _server_started = True
    threading.Thread(target=run_flask, daemon=True).start()


def _make_back_key_listener(webview):
    """Cree un ecouteur Java (via pyjnius) qui intercepte le bouton
    Retour physique d'Android : si la WebView a un historique de
    navigation (l'utilisateur a change de page dans l'appli), on
    recule d'une page au lieu de fermer l'application. S'il n'y a
    plus d'historique, on laisse Android faire son comportement par
    defaut (fermer l'activite), exactement comme un navigateur le
    ferait sur sa toute premiere page."""
    from jnius import PythonJavaClass, java_method, autoclass

    KeyEvent = autoclass("android.view.KeyEvent")

    class BackKeyListener(PythonJavaClass):
        __javainterfaces__ = ["android/view/View$OnKeyListener"]
        __javacontext__ = "app"

        @java_method("(Landroid/view/View;ILandroid/view/KeyEvent;)Z")
        def onKey(self, view, key_code, event):
            is_back = key_code == KeyEvent.KEYCODE_BACK
            is_down = event.getAction() == KeyEvent.ACTION_DOWN
            if is_back and is_down and webview.canGoBack():
                webview.goBack()
                return True  # evenement consomme : on ne ferme pas l'app
            return False  # pas gere ici : Android applique son comportement normal

    return BackKeyListener()


def show_native_webview(url):
    """Remplace le contenu de l'activite Android par une WebView
    native plein ecran chargeant l'URL donnee. Doit s'executer sur le
    thread d'interface Android (d'ou @run_on_ui_thread) : toucher aux
    vues Android depuis un autre thread plante l'application."""
    from jnius import autoclass
    from android.runnable import run_on_ui_thread

    PythonActivity = autoclass("org.kivy.android.PythonActivity")
    WebView = autoclass("android.webkit.WebView")
    WebViewClient = autoclass("android.webkit.WebViewClient")
    WebChromeClient = autoclass("android.webkit.WebChromeClient")

    @run_on_ui_thread
    def _load():
        activity = PythonActivity.mActivity
        webview = WebView(activity)
        settings = webview.getSettings()
        settings.setJavaScriptEnabled(True)
        settings.setDomStorageEnabled(True)
        # WebViewClient (sans surcharge) garde la navigation DANS la
        # WebView : cliquer sur un lien du site n'ouvre pas Chrome.
        webview.setWebViewClient(WebViewClient())
        webview.setWebChromeClient(WebChromeClient())
        activity.setContentView(webview)
        webview.loadUrl(url)

        # Gestion du bouton Retour : la WebView doit pouvoir recevoir
        # les evenements clavier/touches, d'ou le focus explicite.
        back_listener = _make_back_key_listener(webview)
        webview.setFocusable(True)
        webview.setFocusableInTouchMode(True)
        webview.requestFocus()
        webview.setOnKeyListener(back_listener)

        # On garde des references pour eviter que le garbage collector
        # Python ne recupere ces objets pendant que Java les utilise
        # encore (pyjnius ne le devine pas tout seul).
        app = App.get_running_app()
        app._webview = webview
        app._back_listener = back_listener

    _load()


class PokerApp(App):
    title = "Poker"

    def build(self):
        start_server_once()

        if platform == "android":
            # Le temps que Flask termine de demarrer (tres rapide,
            # mais on laisse une petite marge), on affiche un ecran
            # de chargement minimal avant de basculer sur la WebView.
            Clock.schedule_once(lambda dt: show_native_webview(SERVER_URL), 1.5)
            return Label(text="Chargement de la table de poker...")

        # Sur PC (test hors Android), pas de WebView Android
        # disponible : on ouvre simplement le navigateur habituel.
        import webbrowser

        webbrowser.open(SERVER_URL)
        return Label(text=f"Mode test PC : ouvrez {SERVER_URL} dans votre navigateur.")


if __name__ == "__main__":
    PokerApp().run()
