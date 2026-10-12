"""Tournament-name normalization data for compare_odds.py.

Betclic.pl writes tournament names in a mixed style: well-known cities get a
Polish exonym ("Szanghaj ATP", "Wiedeń", "Bazylea"), everything else keeps the
local name ("Wuhan WTA", "Jinan Challenger", "ITF W50 Quebec City"). Pinnacle
uses the English/local name. So only Polish exonyms need a mapping, not every
city on the calendar.

Rules for editing:
  * keys   - lowercase, ASCII only (diacritics are stripped before lookup:
             "Wiedeń" -> "wieden", "Paryż" -> "paryz", "Seul" -> "seul")
  * values - the name Pinnacle uses, lowercase ASCII; may hold several words
             ("hong kong"), they are split into tokens
  * add a line whenever the report's "Matches to verify manually" section shows
    the same tournament under two different names
"""

# Words that do not identify WHICH tournament it is (tour level, gender, round
# stage, format). ITF grades (M25, W50, W15) and pure numbers are dropped in
# code, as are tokens shorter than 3 letters ("di", "de", "la").
TOURNAMENT_STOPWORDS = {
    # tours / levels
    "atp", "wta", "itf", "utr", "challenger", "tour", "masters", "open",
    "pro", "series", "cup",
    # gender / format (English + Polish as used by Betclic)
    "men", "women", "mens", "womens", "singles", "doubles",
    "kobiety", "mezczyzni", "debel",
    # qualifying
    "qualifiers", "qualification", "qualifying", "qual",
    "kwalifikacje", "kwalifikacja",
    # misc
    "tennis", "tenis", "turniej", "final", "finals", "games", "the",
}

# Polish exonym (Betclic) -> English / local name (Pinnacle).
CITY_ALIASES = {
    # --- Asia ---
    "szanghaj": "shanghai", "pekin": "beijing", "tokio": "tokyo",
    "seul": "seoul", "kanton": "guangzhou", "tiencin": "tianjin",
    "hongkong": "hong kong", "awiw": "aviv", "jerozolima": "jerusalem",
    "stambul": "istanbul", "bombaj": "mumbai", "kair": "cairo",
    "marrakesz": "marrakech",
    # --- Western / Central Europe ---
    "wieden": "vienna", "bazylea": "basel", "zurych": "zurich",
    "lozanna": "lausanne", "genewa": "geneva", "berno": "bern",
    "paryz": "paris", "londyn": "london", "bruksela": "brussels",
    "antwerpia": "antwerp", "gandawa": "ghent", "lieza": "liege",
    "kolonia": "cologne", "monachium": "munich", "sztutgart": "stuttgart",
    "lipsk": "leipzig", "norymberga": "nuremberg", "drezno": "dresden",
    "hanower": "hannover", "praga": "prague", "budapeszt": "budapest",
    "warszawa": "warsaw", "bukareszt": "bucharest", "belgrad": "belgrade",
    "saloniki": "thessaloniki", "ateny": "athens",
    # --- Southern Europe ---
    "rzym": "rome", "mediolan": "milan", "neapol": "naples", "turyn": "turin",
    "florencja": "florence", "wenecja": "venice", "padwa": "padua",
    "genua": "genoa", "bolonia": "bologna", "syrakuzy": "syracuse",
    "sardynia": "sardinia", "madryt": "madrid", "sewilla": "seville",
    "saragossa": "zaragoza", "walencja": "valencia", "majorka": "mallorca",
    "teneryfa": "tenerife", "lizbona": "lisbon",
    # --- Northern / Eastern Europe ---
    "sztokholm": "stockholm", "kopenhaga": "copenhagen", "ryga": "riga",
    "wilno": "vilnius", "minsk": "minsk", "kijow": "kyiv", "charkow": "kharkiv",
    "lwow": "lviv", "moskwa": "moscow", "edynburg": "edinburgh",
    # --- Americas ---
    "nowy": "new", "jork": "york", "meksyk": "mexico",
}