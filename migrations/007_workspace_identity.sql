-- Fynn — Migration 007: firms are identified by a workspace, not a phone number.
--
-- The WhatsApp interface is gone; Fynn is a web application. A firm used to be
-- keyed by the number that messaged it, which is no longer a thing that exists.
-- One workspace per deployment for now, under a fixed id — multi-firm is an
-- authentication problem rather than a schema one, and the child tables already
-- reference an opaque firm_id.

alter table firm_profiles rename column phone to workspace_id;

-- The language column belonged to the chat interface: replies were rendered in
-- English or Chinese. The web interface is English-only for now.
alter table firm_profiles drop column if exists language;
