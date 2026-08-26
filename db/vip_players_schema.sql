-- Jugadores VIP / criticos marcados en NUESTRA base (nivel PERSONA-por-cuenta).
--
-- Una fila por (cuenta, contacto). Alimenta las alertas especiales que pidio el
-- negocio: una de RESUMEN cuando un jugador critico es atendido (quien, para que,
-- calificacion, duracion, motivo) y otra de ESPERA LARGA. Las dos arrancan igual --
-- resolver si el contacto que escribio esta en la lista-- y eso tiene que costar cero.
--
-- POR QUE UNA TABLA PROPIA Y NO UNA COLUMNA EN `contacts`: `contacts` es del ETL y este
-- repo no le escribe ni una fila. Una columna ahi es pedirle a otro proyecto que la
-- respete, y el dia que su upsert reescriba la fila la marca se va en silencio.
--
-- POR QUE `es_vip` SI ESTAR EN LA TABLA YA ES SER VIP: apagar no es borrar. Un jugador
-- que deja de ser critico, o un vinculo dudoso, queda en false CONSERVANDO la referencia.
-- Si se borrara la fila, el proximo dump lo vuelve a meter y la decision se pierde.
--
-- NO GUARDA EL TELEFONO a proposito. Ya vive en `contacts.number`: repetirlo no agrega
-- nada y multiplica el lugar donde habria que ir a borrarlo. La fila se ata por
-- `contact_id`, que es un uuid y fuera de esta base no dice nada de nadie.
--
-- Se llena con scripts/load_jugadores_vip.py desde config/jugadores_vip.json, que a su
-- vez sale de scripts/dump_jugadores_vip.py sobre el reporte del casino.
--
-- Idempotente (IF NOT EXISTS). El loader la asegura sola al arrancar (self-healing,
-- como ensure_indexes); este archivo queda como referencia del esquema.

CREATE TABLE IF NOT EXISTS vip_players (
    account     text        NOT NULL,   -- 'sistemas' | 'datos'
    contact_id  text        NOT NULL,   -- persona (tickets.contact_id, como texto)
    es_vip      boolean     NOT NULL DEFAULT true,  -- apagar sin perder la referencia
    username    text,                   -- el username del CASINO (no existe en el CRM)
    player_id   text,                   -- id del jugador en el casino
    agencia     text,                   -- SortiOficial | OnlySorti | ModoSorti | SortiGo
    ranking     integer,                -- puesto en el reporte ('rank' es reservada en SQL)
    motivo      text,                   -- por que es VIP: GGRx5 | R90 | FREC y sus combinaciones
    confianza   text,                   -- alta | media | baja: cuanto vale el vinculo
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account, contact_id)
);

-- PARCIAL y no comun: la alerta solo pregunta por los ENCENDIDOS, y los apagados no
-- tienen por que ocupar lugar en el arbol.
CREATE INDEX IF NOT EXISTS idx_vip_players_on ON vip_players (account) WHERE es_vip;
