import streamlit as st
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import pydeck as pdk
from staticmap import StaticMap, CircleMarker, Line
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from PIL import Image
from io import BytesIO
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
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
    ["Accueil", "Calcul simple", "Import Excel - données horaires", "Comparaison multi-régulateurs"]
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

def generer_pdf_rapport(titre, donnees, latitude=None, longitude=None, puissance_kw=None):
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
        elements.append(Image(carte_buffer, width=420, height=280))
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

    if puissance_kw is not None:
        elements.append(Paragraph("Schéma indicatif de dimensionnement", styles["Heading2"]))
        schema_buffer = generer_image_schema_dimensionnement(puissance_kw)
        elements.append(Image(schema_buffer, width=420, height=320))
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

    ax.set_title(f"Schéma indicatif - {categorie}\nPuissance : {puissance_kw:.2f} kW")
    ax.set_xlim(-1, largeur_local + 2)
    ax.set_ylim(-1, longueur_local + 1.5)
    ax.set_aspect("equal")
    ax.axis("off")

    fig.savefig(buffer, format="png", dpi=150, bbox_inches="tight")
    plt.close(fig)
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

    pdf = generer_pdf_rapport(
    "Rapport SIMHYDRO - Calcul simple",
    donnees_pdf,
    latitude=latitude,
    longitude=longitude,
    puissance_kw=puissance_affichage_kw
    )

    st.download_button(
        label="Télécharger le rapport PDF",
        data=pdf,
        file_name="rapport_simhydro_calcul_simple.pdf",
        mime="application/pdf"
    )
    puissance_affichage_kw = max(float(puissance_kw), 1)


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
