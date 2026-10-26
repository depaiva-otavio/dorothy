# dorothy
# dorothy

O cadastro registra a faixa etária em `db/pesquisa.db` e seleciona perguntas próprias para cada grupo: 100 para 7 a 8 anos e 20 para cada faixa acima. Todas tratam de introdução à microinformática e têm enunciados objetivos, curtos e adequados à idade. Cada partida recebe quatro perguntas diferentes; as combinações não se repetem nas primeiras 100 partidas de cada faixa e a reserva fica no SQLite para valer entre navegadores e dispositivos.

Para jogar, execute `python3 server.py` e abra `http://127.0.0.1:8000`. O Live Server pode ser usado na porta 5500 com o servidor Python ativo; o cadastro e o jogo usam a API na porta 8000.
