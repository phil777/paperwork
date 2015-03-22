# Write a PDF with 2 bits per pixel grayscale
# Largely inspired from simple-scan 

import array
import random
import zlib
import StringIO


class PDFStream(object):
    def __init__(self, filename):
        self.f = open(filename, "w")
        self.objects = []
        self.ofs = 0
    def start_object(self):
        self.objects.append(self.ofs)
        return len(self.objects)
    def write(self, str):
        self.f.write(str)
        self.ofs += len(str)

def write_gray_pdf(filename, pages):

    n_pages = len(pages)
    pdfid = "%032x" % random.randrange(0, 2**128)

    f = PDFStream(filename)
    
    # Header
    f.write("%PDF-1.3\n")

    # Comment with binary as recommended so file is treated as binary
    f.write("%\xe2\xe3\xcf\xd3\n")

    # Catalog
    catalog_number = f.start_object()
    f.write("%u 0 obj\n" % catalog_number)
    f.write("<<\n")
    f.write("/Type /Catalog\n")
    f.write("/Pages %u 0 R\n" % (catalog_number + 1))
    f.write(">>\n")
    f.write("endobj\n")

    # Pages
    f.write("\n")
    pages_number = f.start_object()
    f.write("%u 0 obj\n" % pages_number)
    f.write("<<\n")
    f.write("/Type /Pages\n")
    f.write("/Kids [")
    for i in xrange(n_pages):
        f.write(" %u 0 R" % (pages_number + 1 + (i*3)))
    f.write(" ]\n")
    f.write("/Count %u\n" % n_pages)
    f.write(">>\n")
    f.write("endobj\n")

    for i,page in enumerate(pages):
        image = page.img.convert("L")
        width,height = image.size
        dpi = 300 # XXX: extract dpi from page
        
        page_width = width * 72.0 / dpi
        page_height = height * 72.0 / dpi

        new_width = (width+3)/4.0
        np = 0
        shift_count = 8
        pix2 = array.array("B")
        for p,pval in enumerate(image.getdata()):
            np <<= 2
            np |= pval/64
            shift_count -= 2
            if p % width == width-1 or shift_count == 0:
                np <<= shift_count
                pix2.append(np)
                np = 0
                shift_count = 8

        color_space = "DeviceGray"
        depth = 2

        # Compress data
        compressed_data = zlib.compress(pix2.tostring())
        jpeg = StringIO.StringIO()
        image.save(jpeg, format="jpeg")
        if jpeg.tell() < len(compressed_data):
            filter = "DCTDecode"
            data = jpeg.getvalue()
        else:
            filter = "FlateDecode"
            data = compressed_data

        # Page
        f.write("\n")
        number = f.start_object()
        f.write("%u 0 obj\n" % number)
        f.write("<<\n")
        f.write("/Type /Page\n")
        f.write("/Parent %u 0 R\n" % pages_number)
        f.write("/Resources << /XObject << /Im%d %u 0 R >> >>\n" % (i, number+1))
        f.write("/MediaBox [ 0 0 %.2f %.2f ]\n" % (page_width, page_height))
        f.write("/Contents %u 0 R\n" % (number+2))
        f.write(">>\n")
        f.write("endobj\n")

        # Page image
        f.write("\n")
        number = f.start_object()
        f.write("%u 0 obj\n" % number)
        f.write("<<\n")
        f.write("/Type /XObject\n")
        f.write("/Subtype /Image\n")
        f.write("/Width %d\n" % width)
        f.write("/Height %d\n" % height)
        f.write("/ColorSpace /%s\n" % color_space)
        f.write("/BitsPerComponent %d\n" % depth)
        f.write("/Length %d\n" % len(data))
        if filter:
            f.write("/Filter /%s\n" % filter)
        f.write(">>\n")
        f.write("stream\n")
        f.write(data)
        f.write("\n")
        f.write("endstream\n")
        f.write("endobj\n")

        # Page content
        command = "q\n%f 0 0 %f 0 0 cm\n/Im%d Do\nQ" % (page_width, page_height, i)
        f.write("\n")
        number = f.start_object()
        f.write("%u 0 obj\n" % number)
        f.write("<<\n")
        f.write("/Length %d\n" % len(command))
        f.write(">>\n")
        f.write("stream\n")
        f.write(command)
        f.write("\n")
        f.write("endstream\n")
        f.write("endobj\n")

    # Info
    f.write("\n")
    info_number = f.start_object()
    f.write("%u 0 obj\n" % info_number)
    f.write("<<\n")
    f.write("/Creator (Paperwork)\n")
    f.write(">>\n")
    f.write("endobj\n")

    # Cross-reference table
    f.write("\n")
    xref_offset = f.ofs
    f.write("xref\n")
    f.write("0 %u\n" % (len(f.objects)+1))
    f.write("0000000000 65535 f \n")
    
    for ofs in f.objects:
        f.write("%010u 00000 n \n" % ofs)

    # Trailer
    f.write("\n")
    f.write("trailer\n")
    f.write("<<\n")
    f.write("/Size %u\n" % (len(f.objects)+1))
    f.write("/Info %u 0 R\n" % info_number)
    f.write("/Root %u 0 R\n" % catalog_number)
    f.write("/ID [<%s> <%s>]\n" % (pdfid, pdfid))
    f.write(">>\n")
    f.write("startxref\n")
    f.write("%u\n" % xref_offset)
    f.write("%%EOF\n")


