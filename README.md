# Ficha OCR

Sistema web em Python com Flask para:

- login de usuários;
- perfis `recepcao`, `faturamento` e `admin`;
- cadastro e troca de senha de usuários pelo administrador;
- upload de PDF apenas pela recepção;
- leitura OCR do PDF para identificar o número do atendimento;
- organização automática do arquivo em uma pasta com o número do atendimento.

## Requisitos

O sistema primeiro tenta ler o texto nativo do PDF. Se o arquivo for um PDF pesquisável, o número do atendimento pode ser encontrado mesmo sem OCR. Para PDFs escaneados/imagem, o OCR exige o **Tesseract OCR** instalado no sistema operacional e disponível no `PATH`. No Windows, o app também procura automaticamente em `C:\Program Files\Tesseract-OCR\tesseract.exe`. Se preferir, você também pode definir a variável de ambiente `TESSERACT_CMD` com o caminho da pasta ou do executável.

## Instalação

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

## Acesso inicial

- URL: `http://127.0.0.1:5000`
- Usuário admin padrão: `admin`
- Senha inicial: `admin123`

## Fluxo dos perfis

### Recepção

- faz login;
- acessa **Enviar PDF**;
- envia um arquivo `.pdf`;
- o sistema executa OCR, identifica o número do atendimento e salva em `storage/atendimentos/<numero>/arquivo.pdf`.

### Faturamento

- faz login;
- consulta o dashboard;
- pode baixar os arquivos já processados.

### Admin

- faz login;
- cria usuários;
- redefine senhas;
- acompanha os uploads pelo dashboard.

## Observações sobre OCR

A busca pelo número do atendimento tenta primeiro ler a camada de texto do PDF com `pypdfium2`. Quando o arquivo não possui texto pesquisável, o sistema faz OCR com `pytesseract` e procura padrões como `Atendimento 123456`.
