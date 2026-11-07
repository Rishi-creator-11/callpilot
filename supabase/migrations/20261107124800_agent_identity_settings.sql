alter table public.agent_configs
    add column stock_voice_id text,
    add column speech_language text not null default 'multi' check (speech_language in ('multi', 'en', 'es')),
    add column speech_keyterms jsonb not null default '[]'::jsonb,
    add column speech_pronunciations jsonb not null default '[]'::jsonb,
    add constraint agent_speech_keyterms_limit
        check (jsonb_typeof(speech_keyterms) = 'array' and jsonb_array_length(speech_keyterms) <= 25),
    add constraint agent_speech_pronunciations_limit
        check (jsonb_typeof(speech_pronunciations) = 'array' and jsonb_array_length(speech_pronunciations) <= 16);

alter table public.organizations
    add column business_address text not null default '' check (char_length(business_address) <= 240),
    add column business_website text not null default '' check (char_length(business_website) <= 500);
