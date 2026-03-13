% Informações e Configurações Iniciais
\NeedsTeXFormat{LaTeX2e}
\newcommand{\templateauthor}{COCD/CGDTI/Ipea}

\DeclareOption*{\PassOptionsToClass{\CurrentOption}{article}}
\ProcessOptions\relax
\LoadClass{article}

\def\fileversion{v0.0.1}
\def\filedate{2023/11/01}

\ProvidesClass{td}[\filedate\space \fileversion\space TD Ipea] 

% Pacotes Necessários
\RequirePackage{booktabs}
\RequirePackage{longtable}
\RequirePackage{array}
\RequirePackage{multirow}
\RequirePackage{wrapfig}
\RequirePackage{colortbl}
\RequirePackage{pdflscape}
\RequirePackage{tabu}
\RequirePackage{threeparttable}
\RequirePackage{threeparttablex}
\RequirePackage[normalem]{ulem}
\RequirePackage{makecell}
\RequirePackage{xcolor}
\RequirePackage{ifthen}
\RequirePackage{amsmath,amssymb}
\RequirePackage{setspace}
\RequirePackage{fancyhdr}
\RequirePackage{sectsty}
\RequirePackage{titlesec}
\RequirePackage{wallpaper}
\RequirePackage{pdfpages} 
\RequirePackage{enumitem} 
\RequirePackage{float}
\RequirePackage{fontspec}
\RequirePackage{caption}
\DeclareCaptionLabelFormat{uppercase}{\MakeUppercase{#1} #2}
\captionsetup{
  skip=2pt,
  labelsep=newline,
  labelformat=uppercase,
  font=singlespacing,
  justification=RaggedRight,
  singlelinecheck=false
}

\setlength{\LTpre}{2pt}
\setlength{\LTpost}{2pt}

% Definição do símbolo de número padrão IPEA
\RequirePackage{xspace}
\RequirePackage[normalem]{ulem}
\newcommand{\numero}{n\textsuperscript{\uline{o}}\xspace}

% TABELA (diferente de QUADRO)

\newcommand{\tabelatd}[4]{% #1 conteúdo | #2 titulo | #3 fonte | #4 label
  \begin{table}[!h]
    \centering
    \refstepcounter{table}
    \par\vspace{12pt}
    \noindent\MakeUppercase{\tablename~\thetable}\par\vspace{2pt}
    \begingroup
      \renewcommand{\\}{\par\noindent\normalfont}%
      \noindent\textbf{\ignorespaces#2}\par
    \endgroup
    \vspace{6pt}
    #1
    \par\smallskip
    {\raggedright\footnotesize #3 \par}
    \label{#4}
  \end{table}
}

\newcommand{\tabelacaption}[1]{%
  \refstepcounter{table}%
  \par\vspace{12pt}
  \noindent\MakeUppercase{\tablename~\thetable}\par\vspace{2pt}
  \begingroup
    \renewcommand{\\}{\par\noindent\normalfont}%
    \noindent\textbf{\ignorespaces#1}\par
  \endgroup
  \vspace{6pt}
}

\newenvironment{tabela}[3]
{%
  \refstepcounter{table}
  \par\vspace{12pt}
  \noindent\MakeUppercase{\tablename~\thetable}\par\vspace{2pt}
  \begingroup
    \renewcommand{\\}{\par\noindent\normalfont}%
    \noindent\textbf{\ignorespaces#2}\par
  \endgroup
  \vspace{6pt}
  \begin{longtable}{|#1|}
  \hline
  #3 \\ \hline
  \endfirsthead
  \hline
  #3 \\ \hline
  \endhead
}
{\end{longtable}}


% QUADRO (diferente de TABELA)
\newcounter{quadro}

% nome do quadro
\newcommand{\quadroname}{Quadro}

% legenda de quadro (mesmo padrão de Tabela)
\newcommand{\quadrocaption}[1]{%
  \refstepcounter{quadro}%
  \par\vspace{12pt}
  \noindent\MakeUppercase{\quadroname~\thequadro}\par\vspace{2pt}
  \begingroup
    \renewcommand{\\}{\par\noindent\normalfont}%
    \noindent\textbf{\ignorespaces#1}\par
  \endgroup
  \vspace{6pt}
}

\newenvironment{quadro}[3] % #1 colunas | #2 titulo | #3 cabeçalho
{%
  \refstepcounter{quadro}
  \par\vspace{2pt}
  \noindent\MakeUppercase{\quadroname~\thequadro}\par\vspace{2pt}
  \noindent\textbf{#2}\par\vspace{2pt}
  \begin{longtable}{|#1|}
  \hline
  #3 \\ \hline
  \endfirsthead
  \hline
  #3 \\ \hline
  \endhead
}
{%
  \\ \hline
  \end{longtable}
}

% fontes, notas e observações
\newcommand{\tdfonte}[1]{%
  \par\smallskip
  {\footnotesize #1\par}
}

% \RequirePackage[style = abnt]{biblatex}


\RequirePackage{citation-style-language}
\cslsetup{
    style = template/custom_2026_associacao-brasileira-de-normas-tecnicas-ipea,
    }

\RequirePackage{iftex}

% Configurações de Fonte e Estilo de Página
\ifPDFTeX
  \RequirePackage[T1]{fontenc}
  \RequirePackage[utf8]{inputenc}
  \RequirePackage{textcomp}
\else
  \RequirePackage{unicode-math}
  \defaultfontfeatures{Scale=MatchLowercase}
  \defaultfontfeatures[\rmfamily]{Ligatures=TeX,Scale=1}
\fi
\RequirePackage{lmodern}
\ifPDFTeX\else  
  \setmainfont[]{Times New Roman}
  \setsansfont[]{Times New Roman}
\fi
\IfFileExists{upquote.sty}{\RequirePackage{upquote}}{}
\IfFileExists{microtype.sty}{
  \RequirePackage[]{microtype}
  \UseMicrotypeSet[protrusion]{basicmath}
}{}

% Configurações de Parágrafo e Espaçamento
\makeatletter
\@ifundefined{KOMAClassName}{
  \IfFileExists{parskip.sty}{
    \RequirePackage{parskip}
  }{
    \setlength{\parindent}{0pt}
    \setlength{\parskip}{6pt plus 2pt minus 1pt}
  }
}{
  \KOMAoptions{parskip=half}
}
\makeatother
\RequirePackage{xcolor}
\RequirePackage[top=25mm,left=25mm,right=25mm,bottom=25mm]{geometry}
\setlength{\emergencystretch}{3em}
\setcounter{secnumdepth}{5}

% Configurações de Títulos, Fontes e Espaçamento Vertical
\ifx\paragraph\undefined\else
  \let\oldparagraph\paragraph
  \renewcommand{\paragraph}[1]{\oldparagraph{#1}\mbox{}}
\fi
\ifx\subparagraph\undefined\else
  \let\oldsubparagraph\subparagraph
  \renewcommand{\subparagraph}[1]{\oldsubparagraph{#1}\mbox{}}
\fi

\providecommand{\tightlist}{%
  \setlength{\itemsep}{0pt}\setlength{\parskip}{0pt}
}
\RequirePackage{longtable,booktabs,array}
\RequirePackage{calc}
\RequirePackage{etoolbox}
\makeatletter
\patchcmd\longtable{\par}{\if@noskipsec\mbox{}\fi\par}{}{}
\makeatother
\IfFileExists{footnotehyper.sty}{\RequirePackage{footnotehyper}}{\RequirePackage{footnote}}
\makesavenoteenv{longtable}
\RequirePackage{graphicx}
\makeatletter
\def\maxwidth{\ifdim\Gin@nat@width>\linewidth\linewidth\else\Gin@nat@width\fi}
\def\maxheight{\ifdim\Gin@nat@height>\textheight\textheight\else\Gin@nat@height\fi}
\makeatother
\setkeys{Gin}{width=\maxwidth,height=\maxheight,keepaspectratio}
\makeatletter
\def\fps@figure{htbp}
\makeatother
\RequirePackage{calc}

% Formatação de Página e Títulos
% Formata numeração da página
\pagestyle{fancy}
\fancyhf{}
\renewcommand\headrulewidth{0pt}
\fancyfoot[R]{\rmfamily\fontsize{11}{13}\selectfont\thepage}
\setlength{\footskip}{20pt}

% Formata estilo dos títulos
%% Recuo e espaçamento número-título
\titleformat{\section}
  {\bfseries}
  {\hspace{1.24cm}\thesection\hspace{0.24em}}
  {0.0em}
  {\MakeUppercase}
\titleformat{name=\section,numberless}
  {\bfseries}
  {\hspace{1.24cm}}
  {0.0em}
  {\MakeUppercase}
  {}
\titleformat{\subsection}
  {\bfseries}
  {\hspace{1.24cm}\thesubsection\hspace{0.24em}}
  {0.0em}
  {}
\titleformat{\subsubsection}
  {\bfseries}
  {\hspace{1.24cm}\thesubsubsection\hspace{0.24em}}
  {0.0em}
  {}
  
%% Tamanho das fontes
\sectionfont{\fontsize{12}{14pt}\selectfont}
\subsectionfont{\fontsize{12}{14pt}\selectfont\bfseries}
\subsubsectionfont{\fontsize{12}{14pt}\selectfont\bfseries}

%% Espaçamento vertical
\titlespacing*{\section}
  {0pt} % Espaçamento à esquerda da seção
  {18pt} % Espaçamento antes da seção
  {0pt} % Espaçamento após a seção
\titlespacing*{\subsection}
  {0pt} % Espaçamento à esquerda da seção
  {18pt} % Espaçamento antes da seção
  {0pt} % Espaçamento após a seção
\titlespacing*{\subsubsection}
  {0pt} % Espaçamento à esquerda da seção
  {18pt} % Espaçamento antes da seção
  {0pt} % Espaçamento após a seção
  
%% Alguns comandos úteis

\makeatletter
\renewcommand{\@makefntext}[1]{%
  \noindent
  \hb@xt@\z@{\hss\@makefnmark}#1
}
\makeatother

% formata títulos

\makeatletter
\renewcommand{\maketitle}{%
  \begin{center}
    \parbox{0.9\textwidth}{%
      \justifying
      \bfseries\MakeUppercase{\@tdtitle}
    }
  \end{center}
  \vspace{12pt}
}
\makeatother

% inclui figuras
\newcommand{\figuratd}[5]{% caminho | titulo | unidade | fonte | label
  \begin{figure}[!h]
    \caption{%
      \textbf{#2}\\
      #3
    }
    \includegraphics[width=\textwidth]{#1}
    \caption*{\footnotesize #4}
    \label{#5}
  \end{figure}
}

% inclui gráficos
\RequirePackage{newfloat}
\DeclareFloatingEnvironment[
    fileext=logr,
    listname={Lista de Gráficos},
    name={Gráfico},
    placement=htbp
]{grafico}

\newcommand{\graficotd}[5]{% caminho | titulo | unidade | fonte | label
  \begin{grafico}[!h]
    \centering
    \caption{%
      \textbf{#2}\\
      #3
    }
    \includegraphics[width=\maxwidth]{#1}
    \par\smallskip
    {\raggedright\footnotesize #4 \par}
    \label{#5}
  \end{grafico}
}

% inclui metadados da publicação
\makeatletter
\newcommand{\tdtitle}[1]{\def\@tdtitle{#1}}
\newcommand{\tdnum}[1]{\def\@tdnum{#1}}
\newcommand{\tdcidade}[1]{\def\@tdcidade{#1}}
\newcommand{\tdano}[1]{\def\@tdano{#1}}
\newcommand{\tdedicao}[1]{\def\@tdedicao{#1}}
\newcommand{\tdjel}[1]{\def\@tdjel{#1}}
\newcommand{\tddoi}[1]{\def\@tddoi{#1}}
\newcommand{\tdissn}[1]{\def\@tdissn{#1}}
\newcommand{\tdpchave}[1]{\def\@tdpchave{#1}}
\newcommand{\tdkwords}[1]{\def\@tdkwords{#1}}
\makeatother

%cria lista de autores
\newcommand{\gerautorlist}{
  \begin{flushright}
    \gerautor
  \end{flushright}
}

\newcommand{\gerautor}{}

\newcommand{\tdautor}[2]{%
  \expandafter\gdef\expandafter\gerautor\expandafter{%
    \gerautor
    \footnotesize #1\footnote{#2} \\[1em]
  }
}

% Define um novo comando para Arial em negrito
\newfontfamily\arialboldfont{Arial Bold}
\newcommand{\textarialbold}[1]{{\arialboldfont #1}}

% Configurações Finais
\makeatletter
\@ifpackageloaded{caption}{}{\RequirePackage{caption}}
\AtBeginDocument{%
\ifdefined\contentsname
  \renewcommand*\contentsname{Índice}
\else
  \newcommand\contentsname{Índice}
\fi
\ifdefined\listfigurename
  \renewcommand*\listfigurename{Lista de Figuras}
\else
  \newcommand\listfigurename{Lista de Figuras}
\fi
\ifdefined\listtablename
  \renewcommand*\listtablename{Lista de Tabelas}
\else
  \newcommand\listtablename{Lista de Tabelas}
\fi
\ifdefined\figurename
  \renewcommand*\figurename{Figura}
\else
  \newcommand\figurename{Figura}
\fi
\ifdefined\tablename
  \renewcommand*\tablename{Tabela}
\else
  \newcommand\tablename{Tabela}
\fi
}

\@ifpackageloaded{float}{}{\RequirePackage{float}}
\floatstyle{ruled}
\@ifundefined{c@chapter}{\newfloat{codelisting}{h}{lop}}{\newfloat{codelisting}{h}{lop}[chapter]}
\floatname{codelisting}{Listagem}
\newcommand*\listoflistings{\listof{codelisting}{Lista de Listagens}}
\makeatother
\makeatletter
\@ifpackageloaded{caption}{}{\RequirePackage{caption}}
\@ifpackageloaded{subcaption}{}{\RequirePackage{subcaption}}
\makeatother
\makeatletter
\@ifpackageloaded{tcolorbox}{}{\RequirePackage[skins,breakable]{tcolorbox}}
\makeatother
\makeatletter
\@ifundefined{shadecolor}{\definecolor{shadecolor}{rgb}{.97, .97, .97}}
\makeatother
\makeatletter
\makeatother
\makeatletter

% Configurações Finais (Continuação)
\ifLuaTeX
\RequirePackage[bidi=basic]{babel}
\else
\RequirePackage[bidi=default]{babel}
\fi
\babelprovide[main,import]{portuguese}
\let\LanguageShortHands\languageshorthands
\def\languageshorthands#1{}
\ifLuaTeX
  \RequirePackage{selnolig}
\fi
\IfFileExists{bookmark.sty}{\RequirePackage{bookmark}}{\RequirePackage{hyperref}}
\IfFileExists{xurl.sty}{\RequirePackage{xurl}}{}
\urlstyle{same}
