import streamlit as st
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import pydeck as pdk
from staticmap import StaticMap, CircleMarker, Line
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image as RLImage
from PIL import Image
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

# Imports optionnels pour les masques proches automatiques
try:
    import requests
except Exception:
    requests = None

# ============================================================
# Simulateur de production hydroélectrique sur réseau d'eau
# Avec mode simple + import Excel de données horaires
# ============================================================

RHO_EAU = 1000  # kg/m³
G = 9.81        # m/s²
BAR_TO_PA = 100000

st.set_page_config(
    page_title="Simulateur hydroélectricité réseau d'eau",
    layout="wide"
)
col_logo, col_titre = st.columns([1, 5])

with col_logo:
    try:
        st.image("logo.png", width=120)
    except:
        pass

with col_titre:
    st.title("SIMHYDRO")
    st.subheader("Simulateur de production hydroélectrique sur réseau d'eau")
    st.caption("Estimation de production en remplacement ou dérivation d'un régulateur de pression")

st.title("Simulateur de production hydroélectrique sur réseau d'eau")
st.caption("Estimation de production en remplacement ou dérivation d'un régulateur de pression")

st.sidebar.header("Mode de calcul")
mode_calcul = st.sidebar.radio(
    "Choisir le mode",
    ["Accueil", "Calcul simple", "Import Excel - données horaires", "Comparaison multi-régulateurs", "Photovoltaïque - bâtiment"]
)

# ============================================================
# Fonctions de calcul
# ============================================================
def calcul_hydro(debit_m3h, pression_amont_bar, pression_aval_bar, pertes_charge_bar, rendement_global):
    debit_m3s = debit_m3h / 3600
    pression_recuperable_bar = np.maximum(
        pression_amont_bar - pression_aval_bar - pertes_charge_bar,
        0
    )
    pression_recuperable_pa = pression_recuperable_bar * BAR_TO_PA
    hauteur_mce = pression_recuperable_pa / (RHO_EAU * G)
    puissance_kw = (RHO_EAU * G * debit_m3s * hauteur_mce * rendement_global) / 1000
    return pression_recuperable_bar, hauteur_mce, puissance_kw

def calcul_pertes_charge(debit_m3h, longueur_m, diametre_mm, rugosite_mm, pertes_singulieres_k=0):
    debit_m3s = debit_m3h / 3600
    diametre_m = diametre_mm / 1000
    rugosite_m = rugosite_mm / 1000

    section = np.pi * diametre_m**2 / 4
    vitesse = debit_m3s / section

    viscosite_cinematique = 1.0e-6
    reynolds = vitesse * diametre_m / viscosite_cinematique

    facteur_f = np.where(
        reynolds < 2300,
        64 / reynolds,
        0.25 / (np.log10((rugosite_m / (3.7 * diametre_m)) + (5.74 / reynolds**0.9)))**2
    )

    perte_lineaire_mce = facteur_f * (longueur_m / diametre_m) * (vitesse**2 / (2 * G))
    perte_singuliere_mce = pertes_singulieres_k * (vitesse**2 / (2 * G))

    perte_totale_mce = perte_lineaire_mce + perte_singuliere_mce
    perte_totale_bar = perte_totale_mce * RHO_EAU * G / BAR_TO_PA

    return perte_totale_bar, perte_totale_mce, vitesse

# ============================================================
# Fonctions photovoltaïques bâtiment
# ============================================================
def facteur_orientation_pv(orientation_deg, inclinaison_deg):
    """Facteur simplifié de correction du productible selon orientation/inclinaison.
    0° = Nord, 90° = Est, 180° = Sud, 270° = Ouest.
    """
    ecart_sud = abs(((orientation_deg - 180 + 180) % 360) - 180)
    facteur_azimut = max(0.55, 1 - 0.35 * (ecart_sud / 180) ** 1.4)
    facteur_inclinaison = max(0.80, 1 - 0.12 * abs(inclinaison_deg - 30) / 60)
    return facteur_azimut * facteur_inclinaison

def calcul_pv(surface_m2, puissance_module_wc, surface_module_m2, latitude, orientation_deg, inclinaison_deg,
              productible_ref_kwh_kwc, performance_ratio, ratio_dc_ac, coeff_ombrage, marge_maintenance_m2,
              coeff_masques_proches=1.0):
    surface_exploitable = max(surface_m2 - marge_maintenance_m2, 0)
    nb_modules = int(surface_exploitable // surface_module_m2) if surface_module_m2 > 0 else 0
    puissance_kwc = nb_modules * puissance_module_wc / 1000
    facteur_orientation = facteur_orientation_pv(orientation_deg, inclinaison_deg)
    productible_specifique = (
        productible_ref_kwh_kwc
        * facteur_orientation
        * coeff_ombrage
        * coeff_masques_proches
        * performance_ratio
    )
    production_kwh_an = puissance_kwc * productible_specifique
    puissance_onduleur_kva = puissance_kwc / ratio_dc_ac if ratio_dc_ac > 0 else 0
    return {
        "surface_exploitable_m2": surface_exploitable,
        "nb_modules": nb_modules,
        "puissance_kwc": puissance_kwc,
        "facteur_orientation": facteur_orientation,
        "coeff_masques_proches": coeff_masques_proches,
        "productible_specifique": productible_specifique,
        "production_kwh_an": production_kwh_an,
        "puissance_onduleur_kva": puissance_onduleur_kva
    }

# ============================================================
# Masques proches PV : bâtiments OSM + obstacles manuels
# ============================================================
def distance_azimut_m(lat1, lon1, lat2, lon2):
    """Distance et azimut entre deux points GPS. Azimut : 0° Nord, 90° Est, 180° Sud."""
    r = 6371000
    phi1, phi2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlambda = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlambda / 2) ** 2
    distance = 2 * r * np.arctan2(np.sqrt(a), np.sqrt(1 - a))
    y = np.sin(dlambda) * np.cos(phi2)
    x = np.cos(phi1) * np.sin(phi2) - np.sin(phi1) * np.cos(phi2) * np.cos(dlambda)
    azimut = (np.degrees(np.arctan2(y, x)) + 360) % 360
    return distance, azimut

def lire_hauteur_batiment_osm(tags, hauteur_defaut_m=10.0):
    """Déduit une hauteur bâtiment à partir des tags OSM : height ou building:levels."""
    if not tags:
        return hauteur_defaut_m
    hauteur = tags.get("height")
    if hauteur:
        try:
            return float(str(hauteur).replace("m", "").replace(",", ".").strip())
        except Exception:
            pass
    niveaux = tags.get("building:levels")
    if niveaux:
        try:
            return float(str(niveaux).replace(",", ".")) * 3.0
        except Exception:
            pass
    return hauteur_defaut_m

@st.cache_data(show_spinner=False, ttl=3600)
def recuperer_batiments_osm(latitude, longitude, rayon_m=250, hauteur_defaut_m=10.0):
    """Récupère les bâtiments OSM proches via Overpass API.
    Limite : OSM contient souvent l'emprise, mais pas toujours la hauteur.
    """
    if requests is None:
        return pd.DataFrame()

    requete = f"""
    [out:json][timeout:25];
    (
      way["building"](around:{rayon_m},{latitude},{longitude});
      relation["building"](around:{rayon_m},{latitude},{longitude});
    );
    out center tags;
    """
    try:
        r = requests.post("https://overpass-api.de/api/interpreter", data={"data": requete}, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return pd.DataFrame()

    batiments = []
    for element in data.get("elements", []):
        centre = element.get("center")
        if not centre:
            continue
        lat_b = centre.get("lat")
        lon_b = centre.get("lon")
        if lat_b is None or lon_b is None:
            continue
        tags = element.get("tags", {})
        distance_m, azimut_deg = distance_azimut_m(latitude, longitude, lat_b, lon_b)
        if distance_m <= 1:
            continue
        hauteur_m = lire_hauteur_batiment_osm(tags, hauteur_defaut_m)
        angle_masque_deg = np.degrees(np.arctan2(hauteur_m, distance_m))
        batiments.append({
            "source": "OSM bâtiment",
            "nom": tags.get("name", "Bâtiment OSM"),
            "lat": lat_b,
            "lon": lon_b,
            "distance_m": distance_m,
            "azimut_deg": azimut_deg,
            "largeur_angulaire_deg": max(8, min(35, np.degrees(2 * np.arctan2(10, max(distance_m, 1))))),
            "hauteur_m": hauteur_m,
            "angle_masque_deg": angle_masque_deg,
        })
    return pd.DataFrame(batiments)

def profil_horizon_depuis_obstacles(obstacles_df, pas_azimut=5):
    azimuts = np.arange(0, 360, pas_azimut)
    horizon = pd.DataFrame({"azimut_deg": azimuts, "horizon_deg": np.zeros_like(azimuts, dtype=float)})
    if obstacles_df is None or obstacles_df.empty:
        return horizon

    for _, obs in obstacles_df.iterrows():
        az_c = float(obs["azimut_deg"]) % 360
        demi_largeur = float(obs.get("largeur_angulaire_deg", 15)) / 2
        angle = max(0, float(obs.get("angle_masque_deg", 0)))
        for i, az in enumerate(azimuts):
            ecart = abs(((az - az_c + 180) % 360) - 180)
            if ecart <= demi_largeur:
                horizon.loc[i, "horizon_deg"] = max(horizon.loc[i, "horizon_deg"], angle)
    return horizon

def position_solaire_approchee(latitude, jour_annee, heure_solaire):
    """Position solaire simplifiée suffisante pour un pré-dimensionnement annuel."""
    lat_rad = np.radians(latitude)
    declinaison = np.radians(23.45 * np.sin(np.radians(360 * (284 + jour_annee) / 365)))
    angle_horaire = np.radians(15 * (heure_solaire - 12))
    sin_elev = np.sin(lat_rad) * np.sin(declinaison) + np.cos(lat_rad) * np.cos(declinaison) * np.cos(angle_horaire)
    elev = np.degrees(np.arcsin(np.clip(sin_elev, -1, 1)))
    cos_elev = np.cos(np.radians(elev))
    sin_az = -np.sin(angle_horaire) * np.cos(declinaison) / max(cos_elev, 1e-6)
    cos_az = (np.sin(declinaison) - np.sin(np.radians(elev)) * np.sin(lat_rad)) / (max(cos_elev, 1e-6) * np.cos(lat_rad))
    az = (np.degrees(np.arctan2(sin_az, cos_az)) + 180) % 360
    return elev, az

def coefficient_masque_solaire(latitude, horizon_df):
    """Calcule un coefficient annuel simplifié : 1 = aucun masque, 0.9 = 10 % pertes d'irradiation directe pondérée."""
    if horizon_df is None or horizon_df.empty:
        return 1.0, 0.0
    total = 0.0
    perdu = 0.0
    for jour in range(1, 366, 5):
        for heure in np.arange(5, 21, 0.5):
            elev, az = position_solaire_approchee(latitude, jour, heure)
            if elev <= 0:
                continue
            poids = max(np.sin(np.radians(elev)), 0)
            horizon = np.interp(az, horizon_df["azimut_deg"], horizon_df["horizon_deg"], period=360)
            total += poids
            if elev < horizon:
                perdu += poids
    perte = perdu / total if total > 0 else 0
    coeff = max(0.50, min(1.0, 1 - perte))
    return coeff, perte

def afficher_horizon_masques(horizon_df):
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(horizon_df["azimut_deg"], horizon_df["horizon_deg"])
    ax.set_xlabel("Azimut (°) - 180° = Sud")
    ax.set_ylabel("Hauteur de masque (°)")
    ax.set_title("Profil d'horizon des masques proches")
    ax.set_xlim(0, 360)
    ax.grid(True)
    st.pyplot(fig)

def afficher_carte_masques(latitude, longitude, longueur_m, largeur_m, obstacles_df=None):
    m_lat = 1 / 111320
    m_lon = 1 / (111320 * np.cos(np.radians(latitude)))
    demi_l = longueur_m / 2
    demi_w = largeur_m / 2
    df_toiture = pd.DataFrame({
        "polygon": [[
            [longitude - demi_l * m_lon, latitude - demi_w * m_lat],
            [longitude + demi_l * m_lon, latitude - demi_w * m_lat],
            [longitude + demi_l * m_lon, latitude + demi_w * m_lat],
            [longitude - demi_l * m_lon, latitude + demi_w * m_lat],
        ]],
        "nom": ["Zone photovoltaïque"]
    })
    layers = [pdk.Layer(
        "PolygonLayer", data=df_toiture, get_polygon="polygon",
        get_fill_color=[255, 210, 0, 120], get_line_color=[0, 0, 0],
        line_width_min_pixels=2, pickable=True
    )]
    if obstacles_df is not None and not obstacles_df.empty:
        layers.append(pdk.Layer(
            "ScatterplotLayer", data=obstacles_df, get_position="[lon, lat]",
            get_radius=4, get_fill_color=[200, 60, 60, 200],
            get_line_color=[0, 0, 0], line_width_min_pixels=1, pickable=True
        ))
        layers.append(pdk.Layer(
            "TextLayer", data=obstacles_df, get_position="[lon, lat]", get_text="nom",
            get_size=12, get_color=[0, 0, 0], get_pixel_offset="[0, 18]"
        ))
    view_state = pdk.ViewState(latitude=latitude, longitude=longitude, zoom=18, pitch=0)
    st.pydeck_chart(pdk.Deck(
        map_style="https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
        initial_view_state=view_state, layers=layers, tooltip={"text": "{nom}\nHauteur : {hauteur_m} m\nMasque : {angle_masque_deg}°"}
    ))

def afficher_schema_pv(longueur_toiture_m, largeur_toiture_m, nb_modules, surface_module_m2, inclinaison_deg, orientation_deg):
    fig, ax = plt.subplots(figsize=(9, 6))
    ax.add_patch(plt.Rectangle((0, 0), longueur_toiture_m, largeur_toiture_m, fill=False, linewidth=2))
    ax.text(longueur_toiture_m / 2, largeur_toiture_m + 0.5, "Toiture / zone exploitable", ha="center", fontsize=11)

    ratio_module = 1.7 / 1.1
    module_l = min(1.7, longueur_toiture_m / 10)
    module_w = module_l / ratio_module
    espacement = 0.15
    x, y = 0.4, 0.4
    modules_dessines = 0
    while y + module_w < largeur_toiture_m - 0.4 and modules_dessines < nb_modules:
        x = 0.4
        while x + module_l < longueur_toiture_m - 0.4 and modules_dessines < nb_modules:
            ax.add_patch(plt.Rectangle((x, y), module_l, module_w, fill=False, linewidth=1))
            modules_dessines += 1
            x += module_l + espacement
        y += module_w + espacement

    ax.annotate(f"{longueur_toiture_m:.1f} m", xy=(0, -0.4), xytext=(longueur_toiture_m/2, -0.4), ha="center", arrowprops=dict(arrowstyle="<->"))
    ax.annotate(f"{largeur_toiture_m:.1f} m", xy=(-0.4, 0), xytext=(-0.4, largeur_toiture_m/2), va="center", rotation=90, arrowprops=dict(arrowstyle="<->"))
    ax.set_title(f"Schéma indicatif d'implantation PV\nModules affichés : {modules_dessines}/{nb_modules} - Orientation {orientation_deg:.0f}° - Inclinaison {inclinaison_deg:.0f}°")
    ax.set_aspect("equal")
    ax.set_xlim(-1, longueur_toiture_m + 1)
    ax.set_ylim(-1, largeur_toiture_m + 1.5)
    ax.axis("off")
    st.pyplot(fig)

def afficher_carte_pv(latitude, longitude, longueur_m, largeur_m):
    m_lat = 1 / 111320
    m_lon = 1 / (111320 * np.cos(np.radians(latitude)))
    demi_l = longueur_m / 2
    demi_w = largeur_m / 2
    df_toiture = pd.DataFrame({
        "polygon": [[[
            longitude - demi_l * m_lon, latitude - demi_w * m_lat],
            [longitude + demi_l * m_lon, latitude - demi_w * m_lat],
            [longitude + demi_l * m_lon, latitude + demi_w * m_lat],
            [longitude - demi_l * m_lon, latitude + demi_w * m_lat],
        ]],
        "nom": ["Zone photovoltaïque"]
    })
    layer_toiture = pdk.Layer(
        "PolygonLayer", data=df_toiture, get_polygon="polygon",
        get_fill_color=[255, 210, 0, 120], get_line_color=[0, 0, 0],
        line_width_min_pixels=2, pickable=True
    )
    view_state = pdk.ViewState(latitude=latitude, longitude=longitude, zoom=19, pitch=0)
    st.pydeck_chart(pdk.Deck(
        map_style="https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
        initial_view_state=view_state, layers=[layer_toiture], tooltip={"text": "{nom}"}
    ))

def generer_pdf_rapport(titre, donnees, latitude=None, longitude=None, puissance_kw=None, type_rapport="hydro"):
    buffer = BytesIO()

    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = []

    elements.append(Paragraph(titre, styles["Title"]))
    elements.append(Spacer(1, 12))

    elements.append(Paragraph("Rapport automatique de simulation hydroélectrique", styles["Heading2"]))
    elements.append(Spacer(1, 12))

    if latitude is not None and longitude is not None:
        lien_maps = f"https://www.google.com/maps?q={latitude},{longitude}"

        elements.append(Paragraph("Localisation du site", styles["Heading2"]))
        elements.append(Paragraph(f"Latitude : {latitude:.6f}", styles["Normal"]))
        elements.append(Paragraph(f"Longitude : {longitude:.6f}", styles["Normal"]))
        elements.append(Paragraph(f"Lien Google Maps : {lien_maps}", styles["Normal"]))
        elements.append(Spacer(1, 12))

        elements.append(Paragraph("Vue cartographique du site", styles["Heading2"]))
        carte_buffer = generer_image_carte_site(latitude, longitude)
        elements.append(RLImage(carte_buffer, width=420, height=280))
        elements.append(Spacer(1, 12))

    tableau = [["Indicateur", "Valeur"]]

    for cle, valeur in donnees.items():
        tableau.append([str(cle), str(valeur)])

    table = Table(tableau, colWidths=[250, 200])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
        ("PADDING", (0, 0), (-1, -1), 6),
    ]))

    elements.append(table)
    elements.append(Spacer(1, 18))

# Affichage du schéma uniquement pour l'hydro
if puissance_kw is not None and titre.startswith("Rapport SIMHYDRO - Calcul"):
    elements.append(
        Paragraph(
            "Schéma indicatif de dimensionnement hydroélectrique",
            styles["Heading2"]
        )
    )

    schema_buffer = generer_image_schema_dimensionnement(puissance_kw)

    elements.append(
        RLImage(
            schema_buffer,
            width=420,
            height=320
        )
    )

    elements.append(Spacer(1, 12))

    elements.append(Paragraph(
        "Note : les résultats sont indicatifs et doivent être validés par une étude hydraulique détaillée.",
        styles["Normal"]
    ))

    doc.build(elements)

    buffer.seek(0)
    return buffer
def afficher_schema_dimensionnement(puissance_kw):
    fig, ax = plt.subplots(figsize=(8, 7))

    if puissance_kw < 5:
        largeur_local = 3.0
        longueur_local = 4.0
        taille_turbine = 0.5
        taille_armoire = 0.6
        categorie = "Micro-installation"
    elif puissance_kw < 30:
        largeur_local = 4.0
        longueur_local = 5.5
        taille_turbine = 0.8
        taille_armoire = 0.9
        categorie = "Petite installation"
    else:
        largeur_local = 5.5
        longueur_local = 7.0
        taille_turbine = 1.2
        taille_armoire = 1.2
        categorie = "Installation renforcée"

    # Local
    ax.add_patch(plt.Rectangle((0, 0), largeur_local, longueur_local, fill=False, linewidth=2))

    # Conduite forcée
    ax.plot([largeur_local / 2, largeur_local / 2], [-1, longueur_local], linewidth=3)
    ax.text(largeur_local / 2 + 0.1, -0.7, "Conduite forcée", fontsize=9)

    # Turbine
    ax.add_patch(plt.Rectangle(
        (largeur_local / 2 - taille_turbine / 2, longueur_local * 0.45),
        taille_turbine,
        taille_turbine,
        fill=False,
        linewidth=2
    ))
    ax.text(largeur_local / 2 + 0.4, longueur_local * 0.55, "Turbine", fontsize=9)

    # Alternateur
    ax.add_patch(plt.Rectangle(
        (largeur_local / 2 + 0.5, longueur_local * 0.5),
        0.8,
        0.5,
        fill=False,
        linewidth=2
    ))
    ax.text(largeur_local / 2 + 1.4, longueur_local * 0.58, "Alternateur", fontsize=9)

    # Armoire électrique
    ax.add_patch(plt.Rectangle(
        (largeur_local - taille_armoire - 0.4, 0.5),
        taille_armoire,
        taille_armoire * 1.5,
        fill=False,
        linewidth=2
    ))
    ax.text(largeur_local - taille_armoire - 0.5, 0.2, "Armoire électrique", fontsize=9)

    # Canal de fuite
    ax.plot([largeur_local / 2, largeur_local / 2], [longueur_local, longueur_local + 1], linewidth=2)
    ax.text(largeur_local / 2 + 0.1, longueur_local + 0.5, "Canal de fuite", fontsize=9)

    # Dimensions
    ax.annotate(
        f"{largeur_local:.1f} m",
        xy=(0, longueur_local + 0.3),
        xytext=(largeur_local / 2, longueur_local + 0.3),
        ha="center"
    )

    ax.annotate(
        f"{longueur_local:.1f} m",
        xy=(-0.3, 0),
        xytext=(-0.3, longueur_local / 2),
        rotation=90,
        va="center"
    )

    ax.set_title(f"Schéma indicatif de dimensionnement - {categorie}\nPuissance : {puissance_kw:.2f} kW")
    ax.set_xlim(-1, largeur_local + 2)
    ax.set_ylim(-1, longueur_local + 1.5)
    ax.set_aspect("equal")
    ax.axis("off")

    st.pyplot(fig)

def generer_image_schema_dimensionnement(puissance_kw):
    buffer = BytesIO()
    fig, ax = plt.subplots(figsize=(7, 6))

    if puissance_kw < 5:
        largeur_local = 3.0
        longueur_local = 4.0
        taille_turbine = 0.5
        taille_armoire = 0.6
        categorie = "Micro-installation"
    elif puissance_kw < 30:
        largeur_local = 4.0
        longueur_local = 5.5
        taille_turbine = 0.8
        taille_armoire = 0.9
        categorie = "Petite installation"
    else:
        largeur_local = 5.5
        longueur_local = 7.0
        taille_turbine = 1.2
        taille_armoire = 1.2
        categorie = "Installation renforcée"

    ax.add_patch(plt.Rectangle((0, 0), largeur_local, longueur_local, fill=False, linewidth=2))
    ax.plot([largeur_local / 2, largeur_local / 2], [-1, longueur_local], linewidth=3)
    ax.text(largeur_local / 2 + 0.1, -0.7, "Conduite forcée", fontsize=9)

    ax.add_patch(plt.Rectangle(
        (largeur_local / 2 - taille_turbine / 2, longueur_local * 0.45),
        taille_turbine,
        taille_turbine,
        fill=False,
        linewidth=2
    ))
    ax.text(largeur_local / 2 + 0.4, longueur_local * 0.55, "Turbine", fontsize=9)

    ax.add_patch(plt.Rectangle(
        (largeur_local / 2 + 0.5, longueur_local * 0.5),
        0.8,
        0.5,
        fill=False,
        linewidth=2
    ))
    ax.text(largeur_local / 2 + 1.4, longueur_local * 0.58, "Alternateur", fontsize=9)

    ax.add_patch(plt.Rectangle(
        (largeur_local - taille_armoire - 0.4, 0.5),
        taille_armoire,
        taille_armoire * 1.5,
        fill=False,
        linewidth=2
    ))
    ax.text(largeur_local - taille_armoire - 0.5, 0.2, "Armoire électrique", fontsize=9)

    ax.plot([largeur_local / 2, largeur_local / 2], [longueur_local, longueur_local + 1], linewidth=2)
    ax.text(largeur_local / 2 + 0.1, longueur_local + 0.5, "Canal de fuite", fontsize=9)
    # Dimensions largeur
    ax.annotate(
        f"{largeur_local:.1f} m",
        xy=(0, longueur_local + 0.3),
        xytext=(largeur_local / 2, longueur_local + 0.3),
        ha="center",
        fontsize=10,
        arrowprops=dict(arrowstyle="<->")
    )

    # Dimensions longueur
    ax.annotate(
        f"{longueur_local:.1f} m",
        xy=(-0.3, 0),
        xytext=(-0.3, longueur_local / 2),
        rotation=90,
        va="center",
        fontsize=10,
        arrowprops=dict(arrowstyle="<->")
    )

    ax.set_title(f"Schéma indicatif - {categorie}\nPuissance : {puissance_kw:.2f} kW")
    ax.set_xlim(-1, largeur_local + 2)
    ax.set_ylim(-1, longueur_local + 1.5)
    ax.set_aspect("equal")
    ax.axis("off")

    fig.savefig(buffer, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
    buffer.seek(0)
    return buffer

def generer_image_carte_site(latitude, longitude):
    buffer = BytesIO()

    carte = StaticMap(
        600,
        400,
        url_template="https://a.tile.openstreetmap.org/{z}/{x}/{y}.png"
    )

    marqueur_site = CircleMarker((longitude, latitude), "red", 12)
    carte.add_marker(marqueur_site)

    decalage = 0.00008

    contour_local = [
        (longitude - decalage, latitude - decalage),
        (longitude + decalage, latitude - decalage),
        (longitude + decalage, latitude + decalage),
        (longitude - decalage, latitude + decalage),
        (longitude - decalage, latitude - decalage),
    ]

    carte.add_line(Line(contour_local, "blue", 3))

    image = carte.render(zoom=18)
    image.save(buffer, format="PNG")

    buffer.seek(0)
    return buffer

    # ============================================================
# PAGE D'ACCUEIL
# ============================================================
if mode_calcul == "Accueil":

    st.markdown("""
    ## Outil d'aide à l'identification du potentiel hydroélectrique

    Cette application permet d'estimer le potentiel de production hydroélectrique
    en remplacement ou en dérivation d'un régulateur de pression.
    """)

    col1, col2 = st.columns(2)

    with col1:
        st.info("""
        ### Fonctionnalités principales

        - Calcul simple sur un site
        - Import de données horaires Excel
        - Comparaison de plusieurs régulateurs
        - Pertes de charge avancées
        - Cartographie GPS
        - Dimensionnement indicatif
        - Export PDF automatique
        - Estimation économique
        """)

    with col2:
        st.success("""
        ### Informations application

        **Version :** 1.5

        **Développé par :**
        Elian Gauthier

        **Contact :**
        06 03 99 65 67

        **Usage :**
        Outil d'aide à la décision technique.
        """)

    st.warning("""
    ### Notes techniques

    Les résultats fournis sont indicatifs et doivent être validés
    par une étude hydraulique détaillée et une analyse terrain.
    """)
# ============================================================
# MODE 1 : CALCUL SIMPLE
# ============================================================
elif mode_calcul == "Calcul simple":
    st.sidebar.header("Données d'entrée")

    debit_m3h = st.sidebar.number_input("Débit moyen en m3/h", min_value=0.0, value=50.0, step=1.0)
    pression_amont_bar = st.sidebar.number_input("Pression amont", min_value=0.0, value=8.0, step=0.1)
    pression_aval_bar = st.sidebar.number_input("Pression aval souhaitée", min_value=0.0, value=4.0, step=0.1)
    st.sidebar.header("Localisation du site simple")

    latitude = st.sidebar.number_input("Latitude GPS", value=43.7000, format="%.6f")
    longitude = st.sidebar.number_input("Longitude GPS", value=7.2500, format="%.6f")
    st.sidebar.header("Conduite hydraulique")

    longueur_m = st.sidebar.number_input("Longueur de conduite", min_value=0.0, value=100.0, step=10.0)
    diametre_mm = st.sidebar.number_input("Diamètre intérieur", min_value=1.0, value=200.0, step=10.0)

    materiau = st.sidebar.selectbox(
        "Matériau de la conduite",
        ["PVC / PEHD", "Acier neuf", "Fonte", "Béton", "Acier ancien"]
    )

    rugosites = {
        "PVC / PEHD": 0.01,
        "Acier neuf": 0.05,
        "Fonte": 0.25,
        "Béton": 0.30,
        "Acier ancien": 1.00
    }

    rugosite_mm = rugosites[materiau]

    pertes_singulieres_k = st.sidebar.number_input(
        "Coefficient pertes singulières K",
        min_value=0.0,
        value=2.0,
        step=0.5
    )

    pertes_charge_bar, pertes_charge_mce, vitesse_ms = calcul_pertes_charge(
        debit_m3h,
        longueur_m,
        diametre_mm,
        rugosite_mm,
        pertes_singulieres_k
    )

    rendement_turbine = st.sidebar.slider("Rendement turbine", 0, 100, 70, 1) / 100
    rendement_generatrice = st.sidebar.slider("Rendement génératrice", 0, 100, 92, 1) / 100
    rendement_global = rendement_turbine * rendement_generatrice

    heures_fonctionnement = st.sidebar.number_input("Heures de fonctionnement par an", min_value=0, max_value=8760, value=8000, step=100)
    prix_electricite = st.sidebar.number_input("Prix de l'électricité", min_value=0.0, value=0.15, step=0.01)
    investissement = st.sidebar.number_input("Investissement estimé", min_value=0.0, value=50000.0, step=1000.0)
    facteur_co2 = st.sidebar.number_input("Facteur CO₂ évité", min_value=0.0, value=0.052, step=0.001)
   
    
    pression_recuperable_bar, hauteur_mce, puissance_kw = calcul_hydro(
        debit_m3h,
        pression_amont_bar,
        pression_aval_bar,
        pertes_charge_bar,
        rendement_global
    )
    production_kwh_an = puissance_kw * heures_fonctionnement
    gain_euros_an = production_kwh_an * prix_electricite
    co2_evite_kg_an = production_kwh_an * facteur_co2
    tri = investissement / gain_euros_an if gain_euros_an > 0 else None
    donnees_pdf = {
        "Mode": "Calcul simple",
        "Débit moyen": f"{debit_m3h:.2f} m³/h",
        "Pression amont": f"{pression_amont_bar:.2f} bar",
        "Pression aval": f"{pression_aval_bar:.2f} bar",
        "Pertes de charge": f"{pertes_charge_bar:.2f} bar",
        "Puissance estimée": f"{puissance_kw:.2f} kW",
        "Production annuelle": f"{production_kwh_an:.0f} kWh/an",
        "Gain annuel": f"{gain_euros_an:.0f} EUR/an",
        "CO2 évité": f"{co2_evite_kg_an:.0f} kgCO2/an",
        "Temps de retour brut": f"{tri:.1f} ans" if tri is not None else "Non calculable"
    }
    puissance_affichage_kw = max(float(puissance_kw), 1)

    pdf = generer_pdf_rapport(
        "Rapport SIMHYDRO - Photovoltaïque bâtiment",
        donnees_pdf,
        latitude=latitude,
        longitude=longitude
    )

    st.download_button(
        label="Télécharger le rapport PDF",
        data=pdf,
        file_name="rapport_simhydro_calcul_simple.pdf",
        mime="application/pdf"
    )
    


    st.header("Localisation et dimensionnement du site")

    # Dimensionnement selon puissance
    if puissance_affichage_kw < 5:
        largeur_local_m = 3.0
        longueur_local_m = 4.0
        categorie_dimensionnement = "Micro-installation"
        description_dimensionnement = "Local compact : turbine, alternateur et une armoire électrique."
    elif puissance_affichage_kw < 30:
        largeur_local_m = 4.0
        longueur_local_m = 5.5
        categorie_dimensionnement = "Petite installation"
        description_dimensionnement = "Local intermédiaire : turbine, alternateur, protection et armoire électrique."
    else:
        largeur_local_m = 5.5
        longueur_local_m = 7.0
        categorie_dimensionnement = "Installation renforcée"
        description_dimensionnement = "Local renforcé : turbine, alternateur, armoires électriques et transformateur."

    # Conversion mètres vers coordonnées GPS
    m_lat = 1 / 111320
    m_lon = 1 / (111320 * np.cos(np.radians(latitude)))

    demi_largeur = largeur_local_m / 2
    demi_longueur = longueur_local_m / 2

    # Local technique
    df_local = pd.DataFrame({
        "polygon": [[
            [longitude - demi_largeur * m_lon, latitude - demi_longueur * m_lat],
            [longitude + demi_largeur * m_lon, latitude - demi_longueur * m_lat],
            [longitude + demi_largeur * m_lon, latitude + demi_longueur * m_lat],
            [longitude - demi_largeur * m_lon, latitude + demi_longueur * m_lat],
        ]],
        "nom": ["Local technique"]
    })

    # Conduite forcée + canal de fuite
    df_conduites = pd.DataFrame({
        "path": [
            [
                [longitude, latitude - (demi_longueur + 8) * m_lat],
                [longitude, latitude + demi_longueur * m_lat]
            ],
            [
                [longitude, latitude + demi_longueur * m_lat],
                [longitude, latitude + (demi_longueur + 5) * m_lat]
            ]
        ],
        "nom": ["Conduite forcée", "Canal de fuite"]
    })

    # Équipements
    equipements = [
        {
            "nom": "Turbine",
            "lat": latitude,
            "lon": longitude,
            "rayon": 0.45
        },
        {
            "nom": "Alternateur",
            "lat": latitude + 0.7 * m_lat,
            "lon": longitude + 0.7 * m_lon,
            "rayon": 0.35
        },
        {
            "nom": "Armoire électrique",
            "lat": latitude - 1.1 * m_lat,
            "lon": longitude + 1.0 * m_lon,
            "rayon": 0.35
        },
        {
            "nom": "Protection / contrôle",
            "lat": latitude - 1.1 * m_lat,
            "lon": longitude - 1.0 * m_lon,
            "rayon": 0.30
        }
    ]

    if puissance_affichage_kw >= 30:
        equipements.append({
            "nom": "Transformateur",
            "lat": latitude + 1.2 * m_lat,
            "lon": longitude - 1.0 * m_lon,
            "rayon": 0.40
        })

    df_equipements = pd.DataFrame(equipements)

    # Couches carte
    layer_local = pdk.Layer(
        "PolygonLayer",
        data=df_local,
        get_polygon="polygon",
        get_fill_color=[230, 230, 230, 160],
        get_line_color=[60, 60, 60],
        line_width_min_pixels=2,
        pickable=True,
    )

    layer_conduites = pdk.Layer(
        "PathLayer",
        data=df_conduites,
        get_path="path",
        get_color=[0, 80, 220],
        get_width=2,
        width_min_pixels=4,
        pickable=True,
    )

    layer_equipements = pdk.Layer(
        "ScatterplotLayer",
        data=df_equipements,
        get_position="[lon, lat]",
        get_radius="rayon",
        get_fill_color=[220, 80, 40, 220],
        get_line_color=[0, 0, 0],
        line_width_min_pixels=1,
        pickable=True,
    )

    layer_labels = pdk.Layer(
        "TextLayer",
        data=df_equipements,
        get_position="[lon, lat]",
        get_text="nom",
        get_size=13,
        get_color=[0, 0, 0],
        get_pixel_offset="[0, 18]",
        pickable=True,
    )

    view_state = pdk.ViewState(
        latitude=latitude,
        longitude=longitude,
        zoom=20,
        pitch=0,
    )

    st.pydeck_chart(pdk.Deck(
        map_style="https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
        initial_view_state=view_state,
        layers=[
            layer_local,
            layer_conduites,
            layer_equipements,
            layer_labels
        ],
        tooltip={"text": "{nom}"}
    ))

    st.info(
        f"""
        **Dimensionnement : {categorie_dimensionnement}**

        Puissance installée estimée : **{puissance_affichage_kw:.2f} kW**

        Emprise indicative du local : **{largeur_local_m:.1f} m × {longueur_local_m:.1f} m**

        {description_dimensionnement}
        """
    )
    st.header("Schéma indicatif des équipements")
    afficher_schema_dimensionnement(puissance_affichage_kw)

    st.header("Résultats principaux")

    col1, col2, col3, col4 = st.columns(4)

    col1.metric("Pression récupérable nette", f"{pression_recuperable_bar:.2f} bar")
    col2.metric("Hauteur équivalente", f"{hauteur_mce:.1f} mCE")
    col3.metric("Puissance électrique", f"{puissance_kw:.2f} kW")
    col4.metric("Production annuelle", f"{production_kwh_an:,.0f} kWh/an".replace(",", " "))

    col_perte1, col_perte2, col_perte3 = st.columns(3)

    col_perte1.metric("Pertes de charge", f"{pertes_charge_bar:.2f} bar")
    col_perte2.metric("Pertes de charge", f"{pertes_charge_mce:.1f} mCE")
    col_perte3.metric("Vitesse conduite", f"{vitesse_ms:.2f} m/s")
    
    col5, col6, col7 = st.columns(3)
    col5.metric("Gain économique annuel", f"{gain_euros_an:,.0f} €/an".replace(",", " "))
    col6.metric("CO₂ évité", f"{co2_evite_kg_an:,.0f} kgCO₂/an".replace(",", " "))
    col7.metric("Temps de retour brut", f"{tri:.1f} ans" if tri is not None else "Non calculable")

    st.header("Sensibilité au débit")
    if debit_m3h > 0 and pression_recuperable_bar > 0:
        debits = np.linspace(max(1, debit_m3h * 0.2), debit_m3h * 2, 30)
        puissances = []
        productions = []

        for q_m3h in debits:
            _, _, p_kw = calcul_hydro(
                q_m3h,
                pression_amont_bar,
                pression_aval_bar,
                pertes_charge_bar,
                rendement_global
            )
            puissances.append(p_kw)
            productions.append(p_kw * heures_fonctionnement)

        df = pd.DataFrame({
            "Débit (m³/h)": debits,
            "Puissance (kW)": puissances,
            "Production annuelle (kWh/an)": productions
        })

        fig, ax = plt.subplots()
        ax.plot(df["Débit (m³/h)"], df["Puissance (kW)"])
        ax.set_xlabel("Débit (m³/h)")
        ax.set_ylabel("Puissance électrique (kW)")
        ax.set_title("Puissance électrique en fonction du débit")
        ax.grid(True)
        st.pyplot(fig)
        st.dataframe(df, use_container_width=True)

# ============================================================
# MODE 2 : IMPORT EXCEL DONNÉES HORAIRES
# ============================================================
elif mode_calcul == "Import Excel - données horaires":
    st.sidebar.header("Paramètres économiques")
    st.sidebar.header("Localisation du site")

    latitude = st.sidebar.number_input("Latitude GPS", value=43.7000, format="%.6f")
    longitude = st.sidebar.number_input("Longitude GPS", value=7.2500, format="%.6f")
    rendement_turbine = st.sidebar.slider("Rendement turbine", 0, 100, 70, 1) / 100
    rendement_generatrice = st.sidebar.slider("Rendement génératrice", 0, 100, 92, 1) / 100
    rendement_global = rendement_turbine * rendement_generatrice

    prix_electricite = st.sidebar.number_input("Prix de l'électricité", min_value=0.0, value=0.15, step=0.01)
    investissement = st.sidebar.number_input("Investissement estimé", min_value=0.0, value=50000.0, step=1000.0)
    facteur_co2 = st.sidebar.number_input("Facteur CO₂ évité", min_value=0.0, value=0.052, step=0.001)
    st.sidebar.header("Conduite hydraulique")

    longueur_m = st.sidebar.number_input(
        "Longueur de conduite",
        min_value=0.0,
        value=100.0,
        step=10.0,
        key="excel_longueur_m"
    )

    diametre_mm = st.sidebar.number_input(
        "Diamètre intérieur",
        min_value=1.0,
        value=200.0,
        step=10.0,
        key="excel_diametre_mm"
    )

    materiau = st.sidebar.selectbox(
        "Matériau de la conduite",
        ["PVC / PEHD", "Acier neuf", "Fonte", "Béton", "Acier ancien"],
        key="excel_materiau"
    )

    rugosites = {
        "PVC / PEHD": 0.01,
        "Acier neuf": 0.05,
        "Fonte": 0.25,
        "Béton": 0.30,
        "Acier ancien": 1.00
    }

    rugosite_mm = rugosites[materiau]

    pertes_singulieres_k = st.sidebar.number_input(
        "Coefficient pertes singulières K",
        min_value=0.0,
        value=2.0,
        step=0.5,
        key="excel_pertes_singulieres_k"
    )
    st.header("Localisation du site")

    st.header("Import Excel de données horaires")

    st.markdown("""
    Le fichier Excel doit contenir au minimum les colonnes suivantes :

    | date_heure | debit_m3h | pression_amont_bar | pression_aval_bar |
    |---|---:|---:|---:|
    | 01/01/2025 00:00 | 45 | 8.2 | 4.0 |
    | 01/01/2025 01:00 | 42 | 8.1 | 4.0 |

    Chaque ligne représente une heure de fonctionnement.
    """)

    fichier_excel = st.file_uploader(
        "Importer un fichier Excel",
        type=["xlsx", "xls"]
    )

    if fichier_excel is not None:
        try:
            df = pd.read_excel(fichier_excel)

            colonnes_obligatoires = [
                "date_heure",
                "debit_m3h",
                "pression_amont_bar",
                "pression_aval_bar"
            ]

            colonnes_manquantes = [col for col in colonnes_obligatoires if col not in df.columns]

            if colonnes_manquantes:
                st.error("Colonnes manquantes dans le fichier Excel : " + ", ".join(colonnes_manquantes))
                st.stop()

            df["date_heure"] = pd.to_datetime(
                df["date_heure"].astype(str).str.strip(),
                dayfirst=True,
                format="mixed",
                errors="coerce"
            )

            if df["date_heure"].isna().any():
                st.error("Certaines dates n'ont pas pu être lues. Vérifie le format de la colonne date_heure.")
                st.dataframe(df[df["date_heure"].isna()])
                st.stop()

            df = df.sort_values("date_heure")

            # Nettoyage simple
            df = df.dropna(subset=colonnes_obligatoires)
            df = df[df["debit_m3h"] >= 0]
            df = df[df["pression_amont_bar"] >= 0]
            df = df[df["pression_aval_bar"] >= 0]
            df["pertes_charge_bar"], df["pertes_charge_mce"], df["vitesse_ms"] = calcul_pertes_charge(
                df["debit_m3h"],
                longueur_m,
                diametre_mm,
                rugosite_mm,
                pertes_singulieres_k
            )

            df["pression_recuperable_bar"], df["hauteur_mce"], df["puissance_kw"] = calcul_hydro(
                df["debit_m3h"],
                df["pression_amont_bar"],
                df["pression_aval_bar"],
                df["pertes_charge_bar"],
                rendement_global
            )

            # Comme chaque ligne est horaire : énergie = puissance x 1 h
            df["energie_kwh"] = df["puissance_kw"]
            df["gain_euros"] = df["energie_kwh"] * prix_electricite
            df["co2_evite_kg"] = df["energie_kwh"] * facteur_co2

            production_totale = df["energie_kwh"].sum()
            puissance_moyenne = df["puissance_kw"].mean()
            puissance_max = df["puissance_kw"].max()
            puissance_affichage_kw = max(float(puissance_max), 1)
            pression_moyenne_recup = df["pression_recuperable_bar"].mean()
            pertes_moyennes = df["pertes_charge_bar"].mean()
            vitesse_moyenne = df["vitesse_ms"].mean()
            debit_moyen = df["debit_m3h"].mean()
            gain_total = df["gain_euros"].sum()
            co2_total = df["co2_evite_kg"].sum()
            heures_analysees = len(df)
            tri = investissement / gain_total if gain_total > 0 else None
            puissance_affichage_kw = max(float(puissance_max), 1)
            st.header("Localisation et dimensionnement du site")

            # Dimensionnement selon puissance
            if puissance_affichage_kw < 5:
                largeur_local_m = 3.0
                longueur_local_m = 4.0
                categorie_dimensionnement = "Micro-installation"
                description_dimensionnement = "Local compact : turbine, alternateur et une armoire électrique."
            elif puissance_affichage_kw < 30:
                largeur_local_m = 4.0
                longueur_local_m = 5.5
                categorie_dimensionnement = "Petite installation"
                description_dimensionnement = "Local intermédiaire : turbine, alternateur, protection et armoire électrique."
            else:
                largeur_local_m = 5.5
                longueur_local_m = 7.0
                categorie_dimensionnement = "Installation renforcée"
                description_dimensionnement = "Local renforcé : turbine, alternateur, armoires électriques et transformateur."

            # Conversion mètres vers coordonnées GPS
            m_lat = 1 / 111320
            m_lon = 1 / (111320 * np.cos(np.radians(latitude)))

            demi_largeur = largeur_local_m / 2
            demi_longueur = longueur_local_m / 2

            # Local technique
            df_local = pd.DataFrame({
                "polygon": [[
                    [longitude - demi_largeur * m_lon, latitude - demi_longueur * m_lat],
                    [longitude + demi_largeur * m_lon, latitude - demi_longueur * m_lat],
                    [longitude + demi_largeur * m_lon, latitude + demi_longueur * m_lat],
                    [longitude - demi_largeur * m_lon, latitude + demi_longueur * m_lat],
                ]],
                "nom": ["Local technique"]
            })

            # Conduite forcée + canal de fuite
            df_conduites = pd.DataFrame({
                "path": [
                    [
                        [longitude, latitude - (demi_longueur + 8) * m_lat],
                        [longitude, latitude + demi_longueur * m_lat]
                    ],
                    [
                        [longitude, latitude + demi_longueur * m_lat],
                        [longitude, latitude + (demi_longueur + 5) * m_lat]
                    ]
                ],
                "nom": ["Conduite forcée", "Canal de fuite"]
            })

            # Équipements
            equipements = [
                {
                    "nom": "Turbine",
                    "lat": latitude,
                    "lon": longitude,
                    "rayon": 0.45
                },
                {
                    "nom": "Alternateur",
                    "lat": latitude + 0.7 * m_lat,
                    "lon": longitude + 0.7 * m_lon,
                    "rayon": 0.35
                },
                {
                    "nom": "Armoire électrique",
                    "lat": latitude - 1.1 * m_lat,
                    "lon": longitude + 1.0 * m_lon,
                    "rayon": 0.35
                },
                {
                    "nom": "Protection / contrôle",
                    "lat": latitude - 1.1 * m_lat,
                    "lon": longitude - 1.0 * m_lon,
                    "rayon": 0.30
                }
            ]

            if puissance_affichage_kw >= 30:
                equipements.append({
                    "nom": "Transformateur",
                    "lat": latitude + 1.2 * m_lat,
                    "lon": longitude - 1.0 * m_lon,
                    "rayon": 0.40
                })

            df_equipements = pd.DataFrame(equipements)

            # Couches carte
            layer_local = pdk.Layer(
                "PolygonLayer",
                data=df_local,
                get_polygon="polygon",
                get_fill_color=[230, 230, 230, 160],
                get_line_color=[60, 60, 60],
                line_width_min_pixels=2,
                pickable=True,
            )

            layer_conduites = pdk.Layer(
                "PathLayer",
                data=df_conduites,
                get_path="path",
                get_color=[0, 80, 220],
                get_width=2,
                width_min_pixels=4,
                pickable=True,
            )

            layer_equipements = pdk.Layer(
                "ScatterplotLayer",
                data=df_equipements,
                get_position="[lon, lat]",
                get_radius="rayon",
                get_fill_color=[220, 80, 40, 220],
                get_line_color=[0, 0, 0],
                line_width_min_pixels=1,
                pickable=True,
            )

            layer_labels = pdk.Layer(
                "TextLayer",
                data=df_equipements,
                get_position="[lon, lat]",
                get_text="nom",
                get_size=13,
                get_color=[0, 0, 0],
                get_pixel_offset="[0, 18]",
                pickable=True,
            )

            view_state = pdk.ViewState(
                latitude=latitude,
                longitude=longitude,
                zoom=20,
                pitch=0,
            )

            st.pydeck_chart(pdk.Deck(
                map_style="https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json",
                initial_view_state=view_state,
                layers=[
                    layer_local,
                    layer_conduites,
                    layer_equipements,
                    layer_labels
                ],
                tooltip={"text": "{nom}"}
            ))

            st.info(
                f"""
                **Dimensionnement : {categorie_dimensionnement}**

                Puissance installée estimée : **{puissance_affichage_kw:.2f} kW**

                Emprise indicative du local : **{largeur_local_m:.1f} m × {longueur_local_m:.1f} m**

                {description_dimensionnement}
                """
            )
            st.header("Schéma indicatif des équipements")
            afficher_schema_dimensionnement(puissance_affichage_kw)
            def generer_image_carte_site(latitude, longitude):
                buffer = BytesIO()

                carte = StaticMap(
                    600,
                    400,
                    url_template="https://a.tile.openstreetmap.org/{z}/{x}/{y}.png"
                )

                marqueur_site = CircleMarker((longitude, latitude), "red", 12)
                carte.add_marker(marqueur_site)

                decalage = 0.00008

                contour_local = [
                    (longitude - decalage, latitude - decalage),
                    (longitude + decalage, latitude - decalage),
                    (longitude + decalage, latitude + decalage),
                    (longitude - decalage, latitude + decalage),
                    (longitude - decalage, latitude - decalage),
                ]

                carte.add_line(Line(contour_local, "blue", 3))

                image = carte.render(zoom=18)
                image.save(buffer, format="PNG")

                buffer.seek(0)
                return buffer
            st.header("Résultats avec données horaires")
            st.metric("Pertes de charge moyennes", f"{pertes_moyennes:.2f} bar")
            st.metric("Vitesse moyenne", f"{vitesse_moyenne:.2f} m/s")
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Heures analysées", f"{heures_analysees:,.0f} h".replace(",", " "))
            col2.metric("Débit moyen", f"{debit_moyen:.1f} m³/h")
            col3.metric("Pression récup. moyenne", f"{pression_moyenne_recup:.2f} bar")
            col4.metric("Puissance moyenne", f"{puissance_moyenne:.2f} kW")

            col5, col6, col7, col8 = st.columns(4)
            col5.metric("Puissance maximale", f"{puissance_max:.2f} kW")
            col6.metric("Production totale", f"{production_totale:,.0f} kWh".replace(",", " "))
            col7.metric("Gain total", f"{gain_total:,.0f} €".replace(",", " "))
            col8.metric("TRI brut", f"{tri:.1f} ans" if tri is not None else "Non calculable")

            st.metric("CO₂ évité", f"{co2_total:,.0f} kgCO₂".replace(",", " "))

            st.header("Graphiques")

            fig1, ax1 = plt.subplots()
            ax1.plot(df["date_heure"], df["debit_m3h"])
            ax1.set_xlabel("Date")
            ax1.set_ylabel("Débit (m³/h)")
            ax1.set_title("Débit horaire")
            ax1.grid(True)
            st.pyplot(fig1)

            fig2, ax2 = plt.subplots()
            ax2.plot(df["date_heure"], df["pression_recuperable_bar"])
            ax2.set_xlabel("Date")
            ax2.set_ylabel("Pression récupérable (bar)")
            ax2.set_title("Pression récupérable horaire")
            ax2.grid(True)
            st.pyplot(fig2)

            fig3, ax3 = plt.subplots()
            ax3.plot(df["date_heure"], df["puissance_kw"])
            ax3.set_xlabel("Date")
            ax3.set_ylabel("Puissance (kW)")
            ax3.set_title("Puissance hydroélectrique horaire")
            ax3.grid(True)
            st.pyplot(fig3)

            st.header("Tableau détaillé")
            st.dataframe(df, use_container_width=True)

            st.header("Export")
            csv = df.to_csv(index=False, sep=";").encode("utf-8")
            donnees_pdf = {
                "Mode": "Import Excel données horaires",
                "Heures analysées": f"{heures_analysees:.0f} h",
                "Débit moyen": f"{debit_moyen:.2f} m³/h",
                "Pression récupérable moyenne": f"{pression_moyenne_recup:.2f} bar",
                "Pertes moyennes": f"{pertes_moyennes:.2f} bar",
                "Vitesse moyenne": f"{vitesse_moyenne:.2f} m/s",
                "Puissance moyenne": f"{puissance_moyenne:.2f} kW",
                "Puissance maximale": f"{puissance_max:.2f} kW",
                "Production totale": f"{production_totale:.0f} kWh",
                "Gain total": f"{gain_total:.0f} EUR",
                "CO2 évité": f"{co2_total:.0f} kgCO2",
                "TRI brut": f"{tri:.1f} ans" if tri is not None else "Non calculable"
            }
            puissance_affichage_kw = max(float(puissance_kw), 1)

            pdf = generer_pdf_rapport("Rapport SIMHYDRO - Données horaires", donnees_pdf)

            st.download_button(
                label="Télécharger le rapport PDF",
                data=pdf,
                file_name="rapport_simhydro_donnees_horaires.pdf",
                mime="application/pdf"
            )

            st.download_button(
                label="Télécharger les résultats horaires en CSV",
                data=csv,
                file_name="resultats_horaires_hydroelectricite.csv",
                mime="text/csv"
            )

        except Exception as e:
            st.error("Erreur lors de la lecture du fichier Excel.")
            st.write(e)
    else:
        st.info("Importe un fichier Excel pour lancer la simulation horaire.")
        

# ============================================================
# MODE PV : PHOTOVOLTAÏQUE BÂTIMENT
# Avec masques proches : bâtiments OSM + obstacles manuels
# ============================================================
elif mode_calcul == "Photovoltaïque - bâtiment":
    st.sidebar.header("Données bâtiment")
    surface_toiture_m2 = st.sidebar.number_input("Surface de toiture disponible (m²)", min_value=0.0, value=500.0, step=10.0)
    longueur_toiture_m = st.sidebar.number_input("Longueur indicative de la zone PV (m)", min_value=1.0, value=25.0, step=1.0)
    largeur_toiture_m = st.sidebar.number_input("Largeur indicative de la zone PV (m)", min_value=1.0, value=20.0, step=1.0)
    marge_maintenance_m2 = st.sidebar.number_input("Surface réservée maintenance / obstacles (m²)", min_value=0.0, value=50.0, step=5.0)

    st.sidebar.header("Localisation")
    latitude = st.sidebar.number_input("Latitude GPS", value=43.7000, format="%.6f", key="pv_lat")
    longitude = st.sidebar.number_input("Longitude GPS", value=7.2500, format="%.6f", key="pv_lon")

    st.sidebar.header("Orientation et implantation")
    orientation_deg = st.sidebar.slider("Orientation des panneaux : 180° = Sud", 0, 359, 180, 1)
    inclinaison_deg = st.sidebar.slider("Inclinaison des panneaux", 0, 60, 30, 1)
    coeff_ombrage_general = st.sidebar.slider("Coefficient autres pertes d'ombrage", 0.50, 1.00, 0.98, 0.01)

    st.sidebar.header("Masques proches")
    utiliser_osm = st.sidebar.checkbox("Récupérer automatiquement les bâtiments OpenStreetMap", value=True)
    rayon_osm_m = st.sidebar.slider("Rayon de recherche bâtiments OSM (m)", 50, 500, 250, 25)
    hauteur_defaut_batiment_m = st.sidebar.number_input("Hauteur par défaut bâtiment si inconnue (m)", min_value=3.0, value=10.0, step=1.0)
    hauteur_capteur_m = st.sidebar.number_input("Hauteur approximative des modules/toiture (m)", min_value=0.0, value=6.0, step=0.5)

    st.sidebar.caption("Ajout manuel utile pour arbres, acrotères, cheminées, bâtiments absents d'OSM.")
    nb_obstacles_manuels = st.sidebar.number_input("Nombre d'obstacles manuels", min_value=0, max_value=10, value=0, step=1)

    obstacles_manuels = []
    for i in range(int(nb_obstacles_manuels)):
        with st.sidebar.expander(f"Obstacle manuel {i+1}"):
            nom = st.text_input("Nom", value=f"Obstacle {i+1}", key=f"obs_nom_{i}")
            type_obs = st.selectbox("Type", ["Bâtiment", "Arbre", "Acrotère", "Cheminée", "Équipement toiture", "Autre"], key=f"obs_type_{i}")
            azimut = st.slider("Azimut depuis les panneaux (°)", 0, 359, 180, 1, key=f"obs_az_{i}")
            distance = st.number_input("Distance horizontale (m)", min_value=1.0, value=20.0, step=1.0, key=f"obs_dist_{i}")
            hauteur = st.number_input("Hauteur obstacle au-dessus du sol (m)", min_value=0.0, value=12.0, step=0.5, key=f"obs_h_{i}")
            largeur = st.slider("Largeur apparente (°)", 1, 90, 20, 1, key=f"obs_larg_{i}")

            hauteur_relative = max(0, hauteur - hauteur_capteur_m)
            angle_masque = np.degrees(np.arctan2(hauteur_relative, distance))
            dlat = (distance * np.cos(np.radians(azimut))) / 111320
            dlon = (distance * np.sin(np.radians(azimut))) / (111320 * np.cos(np.radians(latitude)))
            obstacles_manuels.append({
                "source": "Manuel",
                "nom": f"{type_obs} - {nom}",
                "lat": latitude + dlat,
                "lon": longitude + dlon,
                "distance_m": distance,
                "azimut_deg": azimut,
                "largeur_angulaire_deg": largeur,
                "hauteur_m": hauteur,
                "angle_masque_deg": angle_masque,
            })

    st.sidebar.header("Modules et onduleurs")
    puissance_module_wc = st.sidebar.number_input("Puissance unitaire module (Wc)", min_value=100.0, value=450.0, step=10.0)
    surface_module_m2 = st.sidebar.number_input("Surface unitaire module (m²)", min_value=0.5, value=2.1, step=0.1)
    productible_ref_kwh_kwc = st.sidebar.number_input("Productible local de référence plein Sud sans masque (kWh/kWc/an)", min_value=500.0, value=1450.0, step=10.0)
    performance_ratio = st.sidebar.slider("Performance Ratio global", 0.60, 0.95, 0.85, 0.01)
    ratio_dc_ac = st.sidebar.slider("Ratio DC/AC panneaux / onduleurs", 1.00, 1.50, 1.25, 0.01)

    st.sidebar.header("Économie")
    prix_electricite = st.sidebar.number_input("Valeur du kWh produit (€ / kWh)", min_value=0.0, value=0.15, step=0.01, key="pv_prix")
    investissement_kwc = st.sidebar.number_input("Investissement estimé (€ / kWc)", min_value=0.0, value=1000.0, step=50.0)
    facteur_co2 = st.sidebar.number_input("Facteur CO₂ évité (kgCO₂/kWh)", min_value=0.0, value=0.052, step=0.001, key="pv_co2")

    # Récupération bâtiments OSM
    df_osm = pd.DataFrame()
    if utiliser_osm:
        with st.spinner("Récupération des bâtiments OpenStreetMap proches..."):
            df_osm = recuperer_batiments_osm(latitude, longitude, rayon_osm_m, hauteur_defaut_batiment_m)
            if not df_osm.empty:
                # Correction : hauteur relative par rapport à la toiture/modules
                df_osm["angle_masque_deg"] = np.degrees(
                    np.arctan2(np.maximum(df_osm["hauteur_m"] - hauteur_capteur_m, 0), df_osm["distance_m"])
                )

    df_manuels = pd.DataFrame(obstacles_manuels)
    obstacles_df = pd.concat([df_osm, df_manuels], ignore_index=True) if not df_manuels.empty or not df_osm.empty else pd.DataFrame()

    horizon_df = profil_horizon_depuis_obstacles(obstacles_df, pas_azimut=5)
    coeff_masques_proches, perte_masques_proches = coefficient_masque_solaire(latitude, horizon_df)

    resultats = calcul_pv(
        surface_toiture_m2, puissance_module_wc, surface_module_m2, latitude, orientation_deg, inclinaison_deg,
        productible_ref_kwh_kwc, performance_ratio, ratio_dc_ac, coeff_ombrage_general, marge_maintenance_m2,
        coeff_masques_proches=coeff_masques_proches
    )

    production_kwh_an = resultats["production_kwh_an"]
    puissance_kwc = resultats["puissance_kwc"]
    investissement = puissance_kwc * investissement_kwc
    gain_euros_an = production_kwh_an * prix_electricite
    co2_evite_kg_an = production_kwh_an * facteur_co2
    tri = investissement / gain_euros_an if gain_euros_an > 0 else None

    st.header("Dimensionnement photovoltaïque bâtiment")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Surface exploitable", f"{resultats['surface_exploitable_m2']:.0f} m²")
    col2.metric("Nombre de modules", f"{resultats['nb_modules']}")
    col3.metric("Puissance installée", f"{puissance_kwc:.1f} kWc")
    col4.metric("Onduleurs conseillés", f"{resultats['puissance_onduleur_kva']:.1f} kVA")

    col5, col6, col7, col8 = st.columns(4)
    col5.metric("Coeff. masques proches", f"{coeff_masques_proches:.3f}")
    col6.metric("Pertes masques proches", f"{perte_masques_proches*100:.1f} %")
    col7.metric("Productible spécifique", f"{resultats['productible_specifique']:.0f} kWh/kWc/an")
    col8.metric("Production annuelle", f"{production_kwh_an:,.0f} kWh/an".replace(",", " "))

    col9, col10, col11 = st.columns(3)
    col9.metric("Gain annuel", f"{gain_euros_an:,.0f} €/an".replace(",", " "))
    col10.metric("CO₂ évité", f"{co2_evite_kg_an:,.0f} kgCO₂/an".replace(",", " "))
    col11.metric("TRI brut", f"{tri:.1f} ans" if tri is not None else "Non calculable")

    st.header("Masques proches détectés / saisis")
    if obstacles_df.empty:
        st.info("Aucun masque proche détecté ou saisi. Le coefficient de masque proche reste à 1.")
    else:
        st.dataframe(obstacles_df[["source", "nom", "distance_m", "azimut_deg", "largeur_angulaire_deg", "hauteur_m", "angle_masque_deg"]], use_container_width=True)

    st.header("Profil d'horizon proche")
    afficher_horizon_masques(horizon_df)

    st.header("Positionnement cartographique")
    afficher_carte_masques(latitude, longitude, longueur_toiture_m, largeur_toiture_m, obstacles_df)

    st.header("Schéma indicatif d'implantation")
    afficher_schema_pv(longueur_toiture_m, largeur_toiture_m, resultats["nb_modules"], surface_module_m2, inclinaison_deg, orientation_deg)

    st.header("Sensibilité à l'orientation")
    orientations = np.arange(0, 360, 10)
    productions = []
    for ori in orientations:
        r = calcul_pv(surface_toiture_m2, puissance_module_wc, surface_module_m2, latitude, ori, inclinaison_deg,
                      productible_ref_kwh_kwc, performance_ratio, ratio_dc_ac, coeff_ombrage_general, marge_maintenance_m2,
                      coeff_masques_proches=coeff_masques_proches)
        productions.append(r["production_kwh_an"])
    df_sensibilite = pd.DataFrame({"Orientation (°)": orientations, "Production annuelle (kWh/an)": productions})
    fig, ax = plt.subplots()
    ax.plot(df_sensibilite["Orientation (°)"], df_sensibilite["Production annuelle (kWh/an)"])
    ax.set_xlabel("Orientation (°) - 180° = Sud")
    ax.set_ylabel("Production annuelle (kWh/an)")
    ax.set_title("Sensibilité du productible à l'orientation avec masques proches")
    ax.grid(True)
    st.pyplot(fig)
    st.dataframe(df_sensibilite, use_container_width=True)

    st.header("Export")
    donnees_pdf = {
        "Mode": "Photovoltaïque bâtiment avec masques proches",
        "Surface toiture disponible": f"{surface_toiture_m2:.0f} m²",
        "Surface exploitable": f"{resultats['surface_exploitable_m2']:.0f} m²",
        "Nombre de modules": f"{resultats['nb_modules']}",
        "Puissance installée": f"{puissance_kwc:.1f} kWc",
        "Puissance onduleurs": f"{resultats['puissance_onduleur_kva']:.1f} kVA",
        "Orientation": f"{orientation_deg:.0f}°",
        "Inclinaison": f"{inclinaison_deg:.0f}°",
        "Coefficient masques proches": f"{coeff_masques_proches:.3f}",
        "Pertes masques proches": f"{perte_masques_proches*100:.1f} %",
        "Productible spécifique": f"{resultats['productible_specifique']:.0f} kWh/kWc/an",
        "Production annuelle": f"{production_kwh_an:.0f} kWh/an",
        "Gain annuel": f"{gain_euros_an:.0f} EUR/an",
        "CO2 évité": f"{co2_evite_kg_an:.0f} kgCO2/an",
        "TRI brut": f"{tri:.1f} ans" if tri is not None else "Non calculable"
    }
    pdf = generer_pdf_rapport("Rapport SIMHYDRO - Photovoltaïque bâtiment", donnees_pdf, latitude=latitude, longitude=longitude, puissance_kw=max(puissance_kwc, 1))
    st.download_button("Télécharger le rapport PDF", data=pdf, file_name="rapport_pv_batiment.pdf", mime="application/pdf")

    csv = df_sensibilite.to_csv(index=False, sep=";").encode("utf-8")
    st.download_button("Télécharger la sensibilité orientation CSV", data=csv, file_name="sensibilite_orientation_pv.csv", mime="text/csv")

    if not obstacles_df.empty:
        csv_obs = obstacles_df.to_csv(index=False, sep=";").encode("utf-8")
        st.download_button("Télécharger les masques proches CSV", data=csv_obs, file_name="masques_proches_pv.csv", mime="text/csv")

    st.warning("Méthode de pré-dimensionnement : les bâtiments OSM peuvent ne pas avoir de hauteur fiable, et les arbres ne sont pas détectés automatiquement. Pour une étude exécution, valider avec relevé terrain, LiDAR/drone, PVsyst ou PVGIS.")


st.markdown("""
---
### Notes techniques
La puissance est calculée avec :

P = ρ × g × Q × H × η

Avec :
- Q en m³/s ;
- H en mCE ;
- η = rendement turbine × rendement génératrice.

La hauteur équivalente est obtenue à partir de la pression récupérable :

H = ΔP / (ρ × g)
""")
