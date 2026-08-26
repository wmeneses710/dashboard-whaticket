-- Rastro de las alertas de jugador VIP ya enviadas: la idempotencia.
--
-- POR QUE EXISTE. El worker barre cada 60 segundos. Sin este rastro, un VIP que lleva
-- diez minutos esperando genera una alerta POR CICLO: diez mensajes por el mismo hecho.
-- Un canal que grita dos veces por lo mismo se deja de leer, y eso apaga las dos alertas.
--
-- LA CLAVE ES EL EPISODIO, NO EL JUGADOR:
--   espera   `{ticket_id}:{instante del ultimo mensaje del cliente}`. Si fuera solo el
--            ticket, un cliente que vuelve a esperar mañana en la misma conversacion no
--            volveria a alertar nunca.
--   resumen  el `session_id`. Un rescore masivo --como el de v22-- no puede volver a
--            avisar de una charla de hace un mes.
--
-- SE MARCA ANTES DE MANDAR. Al reves, un fallo de red despues del envio dejaria la alerta
-- sin rastro y el proximo barrido la repetiria. Se prefiere perder un aviso a repetirlo.
--
-- Idempotente (IF NOT EXISTS). `src/alertas.ensure_table` la asegura sola en cada barrido
-- (self-healing, como el resto del repo); este archivo queda como referencia del esquema.

CREATE TABLE IF NOT EXISTS alertas_enviadas (
    account    text        NOT NULL,   -- 'sistemas' | 'datos'
    tipo       text        NOT NULL,   -- 'espera' | 'resumen'
    clave      text        NOT NULL,   -- el EPISODIO (ver arriba), nunca el jugador
    enviada_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, tipo, clave)
);

-- Para poder podar el rastro viejo sin escanear la tabla entera.
CREATE INDEX IF NOT EXISTS idx_alertas_enviadas_at ON alertas_enviadas (enviada_at);
