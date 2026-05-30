create extension if not exists vector;

drop function if exists public.match_pathways(vector, float, int);
drop function if exists public.match_pathways(vector(1536), float, int);
drop function if exists public.match_pathways(vector(384), float, int);

alter table public.program_pathways
    drop column if exists embedding;

alter table public.program_pathways
    add column embedding vector(384);

create or replace function public.match_pathways(
    query_embedding vector(384),
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