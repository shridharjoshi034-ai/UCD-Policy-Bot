import pdf_to_md
import ingestion


def initiate_ingestion():
    pdf_to_md.convert_pdf_to_markdown()
    ingestion.ingest()



if __name__ == "__main__":
    initiate_ingestion()