"""Tests de src/censura.py: enmascarar dato sensible EN LA SALIDA.

POR QUE EXISTE. El tablero se sirve en un dominio publico y tiene 14 endpoints de
lectura ANONIMOS que devuelven `customer_number` y transcripts completos (auditoria del
2026-08-24). MEDIDO sobre los 52.135 mensajes de las sesiones scoreadas:
    celular EC                    3.734 (7,16%)   cliente 533 · operador 3.201
    10 digitos (cedula o cuenta)  2.186 (4,19%)   cliente 1.288 · operador 898
    cuenta bancaria                 685 (1,31%)
    cedula declarada                612 (1,17%)
    CREDENCIALES EN CLARO           399 (0,77%)   operador 384   <- lo mas grave
    correo                          354 (0,68%)

DONDE VA, Y ES LA DECISION QUE IMPORTA: en la SALIDA, no en la base ni antes del
scoring. Las señales deterministas viven de estos mismos digitos --  `es_traspaso`
compara el tail del telefono contra el mapa de lineas, `_es_traspaso_de_datos` busca el
email o la cedula, `operator_sent_credentials` busca el patron de credenciales,
`datos_completos_del_alta` exige correo Y digitos--. Censurar antes de calificar no
protege a nadie y rompe la mitad de las rubricas.

LOS MONTOS NO SE CENSURAN A PROPOSITO: "$5" no identifica a nadie y el supervisor lo
necesita para juzgar un deposito. Un dato que no expone y que hace falta no se tapa.
"""
from src.censura import censurar_texto, enmascarar


# --- la forma que pidio el negocio: 8*******1 --------------------------------

def test_enmascarar_deja_el_primero_y_el_ultimo():
    assert enmascarar("812345671") == "8*******1"
    assert enmascarar("0967159807") == "0********7"


def test_enmascarar_no_revela_los_tokens_muy_cortos():
    # Con 4 caracteres, mostrar los dos extremos revela la mitad. Se tapa entero.
    assert enmascarar("1234") == "****"
    assert enmascarar("12") == "**"


def test_enmascarar_conserva_el_largo():
    """El largo es informacion util y NO identifica: distingue una cedula de 10 de un
    celular de 10 de una cuenta de 8, y deja ver que el dato estaba ahi."""
    for s in ("0967159807", "812345671", "12345678"):
        assert len(enmascarar(s)) == len(s)


# --- que censura el texto ----------------------------------------------------

def test_censura_el_celular_y_la_cedula():
    out = censurar_texto("mi numero es 0967159807 y mi cedula 1712345678")
    assert "0967159807" not in out and "1712345678" not in out
    assert "0********7" in out and "1********8" in out
    assert "mi numero es" in out and "mi cedula" in out   # el texto sigue legible


def test_censura_el_correo_pero_deja_ver_que_es_un_correo():
    out = censurar_texto("mandame a ericklopezjosediaz@gmail.com")
    assert "ericklopezjosediaz" not in out
    assert "@" in out, "sin el arroba el lector no sabe que habia un correo"


def test_censura_las_credenciales_en_claro():
    """384 mensajes de operadores entregan usuario y clave por chat. Es el dato mas
    sensible del corpus: con eso se entra a la cuenta de una persona."""
    out = censurar_texto("su usuario es Paula2026 y la contraseña es Sorti2026.")
    assert "Paula2026" not in out
    assert "Sorti2026" not in out
    assert "usuario" in out and "contraseña" in out


def test_NO_censura_los_montos():
    out = censurar_texto("la recarga minima es de $5 y el bono es de $5,00")
    assert "$5" in out


def test_NO_toca_el_texto_comun():
    limpio = "Hola amiga, con gusto te ayudo con tu registro"
    assert censurar_texto(limpio) == limpio


def test_es_idempotente():
    """La salida ya censurada puede volver a pasar por aca (un endpoint que compone
    otro). Enmascarar dos veces no puede comerse mas caracteres."""
    una = censurar_texto("mi numero es 0967159807")
    assert censurar_texto(una) == una


def test_tolera_none_y_vacio():
    assert censurar_texto(None) == ""
    assert censurar_texto("") == ""


# --- PEDIR NO ES ENTREGAR ----------------------------------------------------
#
# Mi primera version tapaba lo que seguia a "usuario"/"contraseña" SIEMPRE, y sobre los
# mensajes reales de la copia se comia palabras comunes:
#     "me ayudas con tu usuario porfa"       -> tapaba "porfa"
#     "Ayudame con tu usuario bro porfa"     -> tapaba "bro"
#     "Su contraseña es tal cual su usuario" -> tapaba "tal"
# La distincion YA estaba resuelta en el repo: `signals.CREDENTIALS_PATTERN` exige el
# orden `su X es` / `usuario: X`, y su comentario lo dice explicito -- "me ayuda con su
# usuario", "cual es su clave" e "indiqueme su usuario" NO matchean. Se reusa esa
# compuerta en vez de inventar otra.

def test_pedir_el_usuario_no_tapa_la_palabra_que_sigue():
    for pedido in ("Hola que tal me ayudas con tu usuario porfa",
                   "Ayudame con tu usuario bro porfa",
                   "tienes el usuario de tu cuenta?",
                   "me ayudas con tu usuario porfa junto a tu correo"):
        assert censurar_texto(pedido) == pedido, pedido


def test_entregar_el_usuario_SI_lo_tapa():
    out = censurar_texto("Muy bien su usuario es Paula2026 y la contraseña es Sorti2026.")
    assert "Paula2026" not in out and "Sorti2026" not in out


def test_la_forma_con_dos_puntos_tambien_se_tapa():
    out = censurar_texto("Tu usuario es: Paula2026\nContraseña: Sorti2026.")
    assert "Paula2026" not in out and "Sorti2026" not in out


def test_la_frase_sin_valor_no_se_toca():
    """`Su contraseña es tal cual su usuario` no entrega nada: describe una regla. Antes
    tapaba "tal" y dejaba la frase incomprensible."""
    frase = "Su contraseña es tal cual su usuario, para que ingrese"
    assert censurar_texto(frase) == frase


def test_es_idempotente_TAMBIEN_con_el_correo():
    """El agujero que el test de idempotencia original no vio, porque probaba con un
    telefono. MEDIDO sobre los 25.282 mensajes distintos de la copia: **345 no eran
    idempotentes**, y todos por el correo.

    LA CAUSA: `*` no esta en `[\\w.+-]`, asi que en la segunda pasada el patron capturaba
    solo los caracteres de palabra pegados al arroba (la `z` final de `e****...z`) y se
    los comia. El dato ya estaba tapado; lo que se degradaba era la senal de que ahi
    habia un correo.
    """
    una = censurar_texto("mandame a ericklopezjosediaz@gmail.com")
    assert censurar_texto(una) == una
    # Y tres pasadas tampoco lo mueven.
    assert censurar_texto(censurar_texto(una)) == una


def test_es_idempotente_con_credenciales_y_digitos():
    for texto in ("su usuario es Paula2026 y la contraseña es Sorti2026.",
                  "mi cedula 1712345678 y mi cuenta 2101059380",
                  "escribime al +593991701676 o al 0967159807"):
        una = censurar_texto(texto)
        assert censurar_texto(una) == una, texto


# --- EL NOMBRE Y EL APELLIDO DENTRO DEL MENSAJE ------------------------------
#
# Pedido del negocio el 2026-08-26. Hasta hoy el nombre se dejaba pasar porque "detectar
# un nombre en texto libre necesita una lista". LA MEDICION SOBRE LA COPIA DIO VUELTA ESA
# PREMISA: casi todo lo que hay que tapar viene con ETIQUETA, en el formulario de retiro.
#
#     Monto a retirar: 15
#     Nombre completo del titular de la cuenta: Domenica Emperatriz Vera Olaya
#     Cedula o DNI: 120856743-6
#
# Es la MISMA forma que las credenciales, que este modulo ya resuelve: etiqueta, separador
# y valor. No hay que adivinar que palabra es un nombre.
#
# MEDIDO sobre los 69.394 mensajes que el tablero puede servir (los de cualquier sesion
# con al menos una conversacion scoreada): **1.368 tocados (1,97%), 0 no idempotentes**, y
# de los 590 formularios que venian LLENOS se tapan **582 (98,6%)**.
#
# LA COLUMNA `contacts.name` NO SE USA, y el negocio dio el motivo: casi nunca es un
# nombre de persona (son usuarios de Facebook e Instagram, publicos de por si). Hay uno
# mas fuerte todavia: el TITULAR de la cuenta bancaria suele ser OTRA persona -- la madre,
# el hermano --, asi que la columna no lo contiene ni aunque quisiera.

def test_tapa_el_nombre_detras_de_la_etiqueta_del_formulario():
    out = censurar_texto("Titular de la cuenta: jean carlos arguello\nCédula: 0954285409")
    assert "jean carlos arguello" not in out
    assert "Titular de la cuenta:" in out, "la etiqueta se queda: dice QUE se tapo"


def test_tapa_el_nombre_partido_en_dos_campos():
    """El formulario de retiro parte el dato: los nombres en un campo y los apellidos en
    otro. Es justo el caso que el negocio llamo peligroso -- dos nombres en una linea y
    dos apellidos en la de abajo."""
    out = censurar_texto("Nombres: Sandra Beatriz \nApellidos:  Saraguro Muñoz")
    for crudo in ("Sandra", "Beatriz", "Saraguro", "Muñoz"):
        assert crudo not in out, crudo


def test_una_sola_palabra_DETRAS_DE_LA_ETIQUETA_si_se_tapa():
    """El piso de dos palabras es del heuristico de linea suelta, NO de la etiqueta.
    `Apellido: Mendoza` es un apellido porque el formulario lo dice, y la otra mitad esta
    en el campo de al lado."""
    out = censurar_texto("Nombre :Liceth \nApellido : Mendoza \nCédula: 1717900326")
    assert "Liceth" not in out and "Mendoza" not in out


def test_la_etiqueta_de_al_lado_sobrevive_al_valor():
    """`Nombre: X Cédula: 1234` viene todo en una linea. Si el valor corre libre se come
    la palabra `Cédula` y la fila queda ilegible."""
    out = censurar_texto("Nombre: Mendoza López Luiggi Cédula: 1317021333")
    assert "Mendoza" not in out and "Luiggi" not in out
    assert "Cédula:" in out


def test_tapa_el_nombre_en_la_plantilla_de_banco_con_negrita():
    """La plantilla que manda el OPERADOR: `*Nombre:* Katty ... *Cédula:* 131...`. El
    asterisco de negrita va entre los dos puntos y el valor."""
    out = censurar_texto("*Nombre:* Katty Lisbeth Miranda Calderon *Cédula:*  1313932822")
    assert "Katty" not in out and "Calderon" not in out


def test_tapa_el_formulario_VERTICAL_con_el_valor_abajo():
    out = censurar_texto("*Nombres:*\nAngelo José \n\n*Apellidos:*\nVera Castro")
    for crudo in ("Angelo", "José", "Vera", "Castro"):
        assert crudo not in out, crudo


def test_tapa_la_etiqueta_SIN_dos_puntos_cuando_ocupa_su_propia_linea():
    """`Nombres Gema Moran Espinales` en su linea. Fuera de la linea esto seria veneno --
    "el titular enviado no deja que veamos" no lleva separador y se comeria la frase --,
    pero anclado al arranque de la linea la prosa no entra."""
    out = censurar_texto("Monto a retirar: $600\nNombres Gema Moran Espinales\n"
                         "Cédula 1313468181\nBanco Pichincha")
    assert "Gema" not in out and "Espinales" not in out
    assert "Banco Pichincha" in out


def test_tapa_el_nombre_SUELTO_sin_ninguna_etiqueta():
    """El caso que no tiene etiqueta: el nombre solo, en su linea, dentro de un mensaje que
    ya trae la cedula y la cuenta. Son 219 lineas en la copia."""
    out = censurar_texto("Agencia: Moreira\nMonto:  34\nBanco Guayaquil\nCta ahorro\n"
                         "0035888034\nMoreira Castillo Guilber Paul \nCédula: 1313059592")
    assert "Guilber" not in out
    assert "Banco Guayaquil" in out, "el vocabulario del formulario no es un nombre"


def test_el_nombre_compuesto_con_conectores_tambien_se_tapa():
    out = censurar_texto("Monto: $315\nBanco Bolivariano\n2001335401\n"
                         "Washington Ariel Villavicencio De La Guerra \nCc: 0503673493")
    assert "Washington" not in out and "Villavicencio" not in out


# --- LO QUE NO SE PUEDE ROMPER -----------------------------------------------

def test_el_prefijo_del_OPERADOR_no_se_toca():
    """El nombre del operador es el EJE del tablero: sin el no hay a quien coachear. Viaja
    en el cuerpo como `*Miguel:*`, que es una etiqueta con dos puntos igual que las del
    formulario -- por eso el patron se ancla en la PALABRA de la etiqueta y no en la forma."""
    for texto in ("*Miguel:*\nMonto a retirar: 24.50",
                  "*Anggie Belén:*\nBANCO PICHINCHA 🟡 Cuenta Corriente: 2100261954"):
        assert "Miguel" in censurar_texto(texto) or "Anggie" in censurar_texto(texto)
    assert censurar_texto("*Miguel:*\nlisto amiga").startswith("*Miguel:*")


def test_la_plantilla_VACIA_no_se_toca():
    """El operador manda el formulario en blanco. No hay nada que tapar, y el valor no
    puede comerse la etiqueta de la linea de abajo."""
    vacia = "Monto a retirar:\nNombres:\nApellidos: \nCédula:\nBanco:\nTipo de cuenta:"
    assert censurar_texto(vacia) == vacia


def test_la_plantilla_de_REGISTRO_no_se_toca():
    plantilla = "Para crear tu cuenta envíame estos datos:\nNombres:\nCorreo electrónico:\nNumero de celular:"
    assert censurar_texto(plantilla) == plantilla


def test_la_bienvenida_de_ATC_no_se_toca():
    """`Tu nombre y apellido` / `Si eres jugador, tu usuario`: la etiqueta sin separador,
    con la linea de abajo empezando por otra cosa. Una version anterior tapaba
    "Si eres jugador"."""
    bienvenida = ("Para poder ayudarte, por favor indícame:\n\n"
                  "Tu nombre y apellido\nSi eres jugador, tu usuario\n"
                  "Si eres agente, el nombre de tu agencia")
    assert censurar_texto(bienvenida) == bienvenida


def test_la_AGENCIA_no_es_una_persona():
    """`Nombre de Agencia` es un nombre comercial y el supervisor lo necesita."""
    texto = "*Nombre de Agencia:* Isaac09Sorti\n*Monto a retirar:* 10"
    assert censurar_texto(texto) == texto


def test_el_NOMBRE_DE_USUARIO_sigue_siendo_la_credencial():
    """`Nombre de usuario` es la cuenta, no la persona, y `registro` premia que se vea la
    entrega. Lo tapa el patron de credenciales cuando trae digito, no el de nombres."""
    out = censurar_texto("Nombre de usuario: Paula2026")
    assert "Nombre de usuario" in out


def test_la_PROSA_con_titular_no_se_toca():
    for frase in ("el titular enviado no deja que veamos movimientos",
                  "su recarga esta en proceso porque el titular bancario presenta intermitencia",
                  "debes solo pasarme numero de cuenta, el banco destino y titular de la cuenta"):
        assert censurar_texto(frase) == frase, frase


def test_una_linea_de_UNA_palabra_no_alcanza_para_tapar():
    """El negocio puso el piso: un nombre solo no expone. Sin ese piso, `titular incorrecto`
    y cualquier palabra suelta del formulario se tapaban."""
    texto = "Monto a retirar: 20\nCédula: 1712345678\nRicardo\nBanco Pichincha"
    assert "Ricardo" in censurar_texto(texto)


def test_la_linea_suelta_NO_dispara_fuera_del_formulario():
    """El heuristico de linea solo corre donde hay cedula o cuenta. En un chat comun,
    dos palabras seguidas son dos palabras."""
    charla = "buenas amigo\nya quedo resuelto\ngracias vale"
    assert censurar_texto(charla) == charla


def test_es_idempotente_TAMBIEN_con_los_nombres():
    for texto in ("Titular de la cuenta: jean carlos arguello\nCédula: 0954285409",
                  "*Nombres:*\nAngelo José \n\n*Apellidos:*\nVera Castro",
                  "Monto: 34\nBanco Guayaquil\n0035888034\nMoreira Castillo Guilber Paul"):
        una = censurar_texto(texto)
        assert censurar_texto(una) == una, texto
        assert censurar_texto(censurar_texto(una)) == una, texto
