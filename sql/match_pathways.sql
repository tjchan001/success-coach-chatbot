create extension if not exists vector;

alter table public.program_pathways
    add column if not exists embedding vector(1536);

create or replace function public.match_pathways(
    query_embedding vector(1536),
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