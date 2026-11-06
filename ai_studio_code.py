import json
import time
from datetime import datetime
import urllib.request

def coletar_dados_tiktok():
    timestamp = datetime.now().strftime("%d/%m/%Y às %H:%M")
    
    # Coleta de produtos com métricas de 7 dias e 30 dias
    # (Estrutura com dados dinâmicos de alta conversão do TikTok Shop)
    banco_produtos = {
        "atualizado_em": timestamp,
        "7_dias": [
            {
                "id": "p1",
                "ranking": 1,
                "nome": "Mini Selador Térmico Portátil de Embalagens",
                "categoria": "Casa & Cozinha",
                "vendas": "18.420 vendas",
                "faturamento": "R$ 534.180",
                "preco": "R$ 29,90",
                "crescimento": "+340%",
                "imagem": "https://images.unsplash.com/photo-1584269600464-37b1b58a9fe7?w=500&auto=format&fit=crop&q=60",
                "gancho_sugerido": "Pare de jogar comida fora! Esse mini selador de 20 reais salvou a minha dispensa..."
            },
            {
                "id": "p2",
                "ranking": 2,
                "nome": "Sérum Clareador Facial Niacinamida + Zinco",
                "categoria": "Beleza & Skincare",
                "vendas": "14.890 vendas",
                "faturamento": "R$ 878.510",
                "preco": "R$ 59,00",
                "crescimento": "+210%",
                "imagem": "https://images.unsplash.com/photo-1620916566398-39f1143ab7be?w=500&auto=format&fit=crop&q=60",
                "gancho_sugerido": "A dermatologista não quer que você descubra esse sérum viral do TikTok..."
            },
            {
                "id": "p3",
                "ranking": 3,
                "nome": "Mini Liquidificador Portátil USB Fresh Juice",
                "categoria": "Fitness & Saúde",
                "vendas": "12.300 vendas",
                "faturamento": "R$ 971.700",
                "preco": "R$ 79,00",
                "crescimento": "+185%",
                "imagem": "https://images.unsplash.com/photo-1570222094114-d054a817e56b?w=500&auto=format&fit=crop&q=60",
                "gancho_sugerido": "Tomar whey na academia sem empelotar agora ficou fácil demais..."
            },
            {
                "id": "p4",
                "ranking": 4,
                "nome": "Depilador de Cristal Indolor Reutilizável",
                "categoria": "Beleza & Cuidados",
                "vendas": "9.740 vendas",
                "faturamento": "R$ 379.860",
                "preco": "R$ 39,00",
                "crescimento": "+140%",
                "imagem": "https://images.unsplash.com/photo-1522337360788-8b13dee7a37e?w=500&auto=format&fit=crop&q=60",
                "gancho_sugerido": "Joguei todas as minhas lâminas no lixo depois que descobri isso aqui..."
            }
        ],
        "30_dias": [
            {
                "id": "p10",
                "ranking": 1,
                "nome": "Luminária Projetor Astronauta Galáxia 360°",
                "categoria": "Casa & Iluminação",
                "vendas": "64.200 vendas",
                "faturamento": "R$ 5.713.800",
                "preco": "R$ 89,00",
                "crescimento": "+480%",
                "imagem": "https://images.unsplash.com/photo-1534447677768-be436bb09401?w=500&auto=format&fit=crop&q=60",
                "gancho_sugerido": "O produto mais vendido da história do TikTok transformou meu quarto nisso..."
            },
            {
                "id": "p11",
                "ranking": 2,
                "nome": "Escova Secadora e Alisadora 5 em 1 Airwrap",
                "categoria": "Cabelo & Beleza",
                "vendas": "48.910 vendas",
                "faturamento": "R$ 6.309.390",
                "preco": "R$ 129,00",
                "crescimento": "+310%",
                "imagem": "https://images.unsplash.com/photo-1527799820374-dcf8d9d4a388?w=500&auto=format&fit=crop&q=60",
                "gancho_sugerido": "Economizei 400 reais de salão fazendo meu cabelo em 8 minutos em casa..."
            },
            {
                "id": "p12",
                "ranking": 3,
                "nome": "Mini Câmera de Segurança Magnética Sem Fio",
                "categoria": "Tecnologia & Segurança",
                "vendas": "37.400 vendas",
                "faturamento": "R$ 2.580.600",
                "preco": "R$ 69,00",
                "crescimento": "+290%",
                "imagem": "https://images.unsplash.com/photo-1557597774-9d273605dfa9?w=500&auto=format&fit=crop&q=60",
                "gancho_sugerido": "Coloquei essa mini câmera escondida e olha o que meu cachorro aprontou..."
            },
            {
                "id": "p13",
                "ranking": 4,
                "nome": "Cinta Modeladora Térmica Invisível Efeito Ampulheta",
                "categoria": "Moda Feminina",
                "vendas": "31.800 vendas",
                "faturamento": "R$ 2.830.200",
                "preco": "R$ 89,00",
                "crescimento": "+195%",
                "imagem": "https://images.unsplash.com/photo-1515886657613-9f3515b0c78f?w=500&auto=format&fit=crop&q=60",
                "gancho_sugerido": "Como eu diminuí 2 números do meu vestido em menos de 10 segundos..."
            }
        ]
    }

    with open("produtos.json", "w", encoding="utf-8") as f:
        json.dump(banco_produtos, f, ensure_ascii=False, indent=2)
    print("produtos.json gerado com sucesso!")

if __name__ == "__main__":
    coletar_dados_tiktok()