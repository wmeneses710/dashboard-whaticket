-- Rastro de las alertas de jugador VIP ya enviadas: la idempotencia.
--
-- POR QUE EXISTE. El worker barre cada 60 segundos. Sin este rastro, la misma charla
-- generaria un aviso POR CICLO. Un canal que dice dos veces lo mismo se deja de leer.
--
-- LA CLAVE ES EL EPISODIO, NO EL JUGADOR:
--   resumen  el `interaccion_id`. Un rescore masivo no puede volver a avisar de una
--            charla de hace un mes. Era el `session_id` hasta el grano INTERACCION
--            (2026-08-27): con N atenciones en una charla, dedupear por sesion dejaba
--            mudas a N-1, cada una de un operador distinto con su propia nota.
--
-- `tipo` HOY VALE SIEMPRE 'resumen'. Hubo un tipo 'espera' (y su marca 'espera_vista')
-- hasta el 2026-08-31: se borro porque el pipeline llega tarde por aritmetica --132
-- esperas superaban el umbral de 5 min en 30 dias y solo 3 habrian llegado--. La columna
-- se conserva porque es parte de la PK y migrarla no gana nada.
--
-- SE MARCA ANTES DE MANDAR. Al reves, un fallo de red despues del envio dejaria la alerta
-- sin rastro y el proximo barrido la repetiria. Se prefiere perder un aviso a repetirlo.
--
-- Idempotente (IF NOT EXISTS). `src/alertas.ensure_table` la asegura sola en cada barrido
-- (self-healing, como el resto del repo); este archivo queda como referencia del esquema.

CREATE TABLE IF NOT EXISTS alertas_enviadas (
    account    text        NOT NULL,   -- 'sistemas' | 'datos'
    tipo       text        NOT NULL,   -- hoy siempre 'resumen' (ver arriba)
    clave      text        NOT NULL,   -- el EPISODIO (ver arriba), nunca el jugador
    enviada_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, tipo, clave)
);

-- Para poder podar el rastro viejo sin escanear la tabla entera.
CREATE INDEX IF NOT EXISTS idx_alertas_enviadas_at ON alertas_enviadas (enviada_at);
