import re
import ingestion
import pymupdf4llm
import os

def convert_pdf_to_markdown():
    SCRIPT_DIR = os.path.dirname(os.path.realpath(__file__))
    PDF_DIR =  os.path.join(SCRIPT_DIR, "policies_pdfs")
    MD_DIR  = os.path.join(SCRIPT_DIR, "policies_mds")

    pdf_files = []

    if not os.path.exists(PDF_DIR):
        print(f" Critical Error: The directory does not exist at: {PDF_DIR}")
        exit(1)

    for filename in os.listdir(PDF_DIR):
        if filename.endswith(".pdf"):
            pdf_files.append(os.path.join(filename))

    for pdf_file in pdf_files:
        pdf_path = os.path.join(PDF_DIR, pdf_file)
        md_file_name =  pdf_file.replace(".pdf", ".md")

        md_staging_path = os.path.join(MD_DIR, md_file_name)
        md_archive_path = os.path.join(SCRIPT_DIR, "policies_mds_old")

        md_archive_file_path = os.path.join(md_archive_path, md_file_name)

        try:
            mdfile_text = pymupdf4llm.to_markdown(pdf_path)
            clean_pattern = r"\*\*==> picture \[\d+ x \d+\] intentionally omitted <==\*\*\n?"
            mdfile_text = re.sub(clean_pattern, "", mdfile_text)

            if validate_markdown(md_archive_file_path, mdfile_text):
                continue
            with open(md_staging_path, "w", encoding="utf-8") as md_file:
                md_file.write(mdfile_text)
            print("Converted", pdf_path, "to", md_file_name)
        except Exception as e:
            print("Error converting", pdf_path, ":", str(e))


def validate_markdown(md_archive_file_path, mdfile_text):
    if os.path.exists(md_archive_file_path):
        with open(md_archive_file_path, "r", encoding="utf-8") as md_file:
            old_mdfile_text = md_file.read()

        if(mdfile_text == old_mdfile_text):
            print("SKIP: No changes detected for", os.path.basename(md_archive_file_path))
            return True

        return False
    #New Policy Detected
    return False


if __name__ == "__main__":
    convert_pdf_to_markdown()