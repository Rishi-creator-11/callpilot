alter table public.knowledge_documents
    add column source_url text
    check (char_length(source_url) <= 2048);

create unique index knowledge_documents_organization_source_idx
    on public.knowledge_documents (organization_id, source_url);
