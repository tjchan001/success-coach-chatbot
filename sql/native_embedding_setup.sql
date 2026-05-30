create extension if not exists vector;
create extension if not exists vault;
create extension if not exists ai;

alter table public.program_pathways
    add column if not exists embedding vector(1536);

create or replace function public.embed_text_native(input_text text)
returns vector(1536)
language plpgsql
security definer
set search_path = public
as $$
declare
    service_api_key text;
begin
    select ds.decrypted_secret
    into service_api_key
    from vault.decrypted_secrets as ds
    where ds.name = 'OPENAI_API_KEY'
    limit 1;

    if service_api_key is null then
        raise exception 'OPENAI_API_KEY secret not found in vault.decrypted_secrets';
    end if;

    return ai.openai_embed(
        'text-embedding-3-small',
        input_text,
        service_api_key
    )::vector(1536);
end;
$$;

create or replace function public.set_program_pathway_embedding()
returns trigger
language plpgsql
as $$
begin
    if new.content is null or btrim(new.content) = '' then
        new.embedding := null;
    else
        new.embedding := public.embed_text_native(new.content);
    end if;
    return new;
end;
$$;

drop trigger if exists trg_program_pathways_embedding on public.program_pathways;

create trigger trg_program_pathways_embedding
before insert or update of content
on public.program_pathways
for each row
execute function public.set_program_pathway_embedding();

create or replace function public.match_pathways(
    query_embedding vector,
    match_threshold float,
    match_count int
)
returns table (
    id bigint,
    program_name text,
    semester_name text,
    content text,
    similarity float
)
language sql
stable
as $$
    select
        p.id,
        p.program_name,
        p.semester_name,
        p.content,
        1 - (p.embedding <=> query_embedding) as similarity
    from public.program_pathways as p
    where p.embedding is not null
      and 1 - (p.embedding <=> query_embedding) >= match_threshold
    order by p.embedding <=> query_embedding
    limit match_count;
$$;