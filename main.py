__author__ = 'dex'
from funcy import cat, first, re_all
import conf, urllib2, os, shutil, gzip, psycopg2, psycopg2.extras, pandas as pd, numpy as np, re
# get a connection, if a connect cannot be made an exception will be raised here
conn = psycopg2.connect(conf.DB_PARAMATERS)
cursor = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)


def __getMatrixNumHeaderLines(inStream):
    import re

    p = re.compile(r'^"ID_REF"')
    for i, line in enumerate(inStream):
        if p.search(line):
            return i


def matrix_filenames(series_id, platform_id):
    gse_name = query_record(series_id, "series")['gse_name']
    yield "%s/%s_series_matrix.txt.gz" % (gse_name, gse_name)

    gpl_name = query_record(platform_id, "platform")['gpl_name']
    yield "%s/%s-%s_series_matrix.txt.gz" % (gse_name, gse_name, gpl_name)


def get_matrix_filename(series_id, platform_id):
    filenames = list(matrix_filenames(series_id, platform_id))
    mirror_filenames = (os.path.join(conf.SERIES_MATRIX_MIRROR, filename) for filename in filenames)
    mirror_filename = first(filename for filename in mirror_filenames if os.path.isfile(filename))
    if mirror_filename:
        return mirror_filename

    for filename in filenames:
        print 'Loading URL', conf.SERIES_MATRIX_URL + filename, '...'
        try:
            res = urllib2.urlopen(conf.SERIES_MATRIX_URL + filename)
        except urllib2.URLError:
            pass
        else:
            mirror_filename = os.path.join(conf.SERIES_MATRIX_MIRROR, filename)
            print 'Cache to', mirror_filename

            directory = os.path.dirname(mirror_filename)
            if not os.path.exists(directory):
                os.makedirs(directory)
            with open(mirror_filename, 'wb') as f:
                shutil.copyfileobj(res, f)

            return mirror_filename

    raise LookupError("Can't find matrix file for series %s, platform %s"
                      % (series_id, platform_id))

def getImputed(data):
    r.library("impute")
    r_data = com.convert_to_r_matrix(data)
    r_imputedData = r['impute.knn'](r_data)
    npImputedData = np.asarray(r_imputedData[0])
    imputedData = pd.DataFrame(npImputedData)
    imputedData.index = data.index
    imputedData.columns = data.columns
    return imputedData

def get_data(series_id, platform_id):
    matrixFilename = get_matrix_filename(series_id, platform_id)
    # setup data for specific platform
    for attempt in (0, 1):
        try:
            headerRows = __getMatrixNumHeaderLines(gzip.open(matrixFilename))
            na_values = ["null", "NA", "NaN", "N/A", "na", "n/a"]
            data = pd.io.parsers.read_table(gzip.open(matrixFilename),
                                            skiprows=headerRows,
                                            index_col=["ID_REF"],
                                            na_values=na_values,
                                            skipfooter=1,
                                            engine='python')
        except IOError as e:
            # In case we have cirrupt file
            print "Failed loading %s: %s" % (matrixFilename, e)
            os.remove(matrixFilename)
            if attempt:
                raise
            matrixFilename = get_matrix_filename(series_id, platform_id)
    data_file_name = "%s_%s.data.csv"
    data = cleanData(data)
    data = get_imputed(data)
    data.index = data.index.astype(str)
    data.index.name = "probe"
    for column in data.columns:
        data[column] = data[column].astype(np.float64)
    # data.to_csv(data_file_name)
    return data


def get_platform_probes(platform_id):
    sql = "select * from platform_probe where platform_id = %s"
    return pd.read_sql(sql, conn, "probe", params=(platform_id,))


def query_platform_probes(gpl_name):
    platform_id = query_record(gpl_name, "platform", "gpl_name")['id']
    return get_platform_probes(platform_id)


def get_samples(series_id, platform_id):
    sql = "select * from sample where series_id = %s and platform_id = %s"
    return pd.read_sql(sql, conn, "id", params=(series_id, platform_id,))


def query_samples(gse_name, gpl_name):
    series_id = query_record(gse_name, "series", "gse_name")['id']
    platform_id = query_record(gpl_name, "platform", "gpl_name")['id']
    return get_samples(series_id, platform_id)


def get_gene_data(series_id, platform_id):
    sample_data = get_data(series_id, platform_id)
    platform_probes = get_platform_probes(platform_id)
    gene_data = platform_probes[['mygene_sym', 'mygene_entrez']] \
        .join(sample_data) \
        .set_index(['mygene_sym', 'mygene_entrez'])
    return gene_data


def query_record(id, table, id_field="id"):
    sql = """select * from %s where %s """ % (table, id_field) + """= %s"""
    cursor.execute(sql, (id,))
    return cursor.fetchone()


def query_gene_data(gse_name, gpl_name):
    series_id = query_record(gse_name, "series", "gse_name")['id']
    platform_id = query_record(gpl_name, "platform", "gpl_name")['id']
    return get_gene_data(series_id, platform_id)


def query_data(gse_name, gpl_name):
    series_id = query_record(gse_name, "series", "gse_name")['id']
    platform_id = query_record(gpl_name, "platform", "gpl_name")['id']
    return get_data(series_id, platform_id)


import rpy2.robjects as robjects

r = robjects.r
import pandas.rpy.common as com


def dropMissingSamples(data, naLimit=0.8):
    """Filters a data frame to weed out cols with missing data"""
    thresh = len(data.index) * (1 - naLimit)
    return data.dropna(thresh=thresh, axis="columns")


def drop_missing_genes(data, naLimit=0.5):
    """Filters a data frame to weed out cols with missing data"""
    thresh = len(data.columns) * (1 - naLimit)
    return data.dropna(thresh=thresh, axis="rows")


def query_median_gene_data(gse_name, gpl_name):
    """returns the median intensity"""
    gene_data = query_gene_data(gse_name, gpl_name)
    gene_data_median = gene_data \
        .reset_index() \
        .groupby(['mygene_sym', 'mygene_entrez']) \
        .median()
    return gene_data_median

def get_combined_matrix(names):
    """returns an averaged matrix of expression values over all supplid gses"""
    gse_name, gpl_name = names[0]
    m = query_median_gene_data(gse_name, gpl_name)
    for (gse_name, gpl_name) in names[1:]:
        median_gene_data = query_median_gene_data(gse_name, gpl_name)
        median_gene_data.to_csv("%s.%s.median.csv"%(gse_name, gpl_name))
        if median_gene_data.empty:
            continue
        m = m.join(median_gene_data,
                   how="inner")
    return m

def getCombinedSamples(names):
    gse_name, gpl_name = names[0]
    combined_samples = query_samples(gse_name, gpl_name)
    combined_samples['gse_name'] = gse_name
    combined_samples['gpl_name'] = gpl_name
    for (gse_name, gpl_name) in names[1:]:
        print gse_name, gpl_name,
        samples = query_samples(gse_name, gpl_name)
        samples['gpl_name'] = gpl_name
        combined_samples = pd.concat([combined_samples, samples])
    return combined_samples


def get_combat(names, labels):
    # drop genes with missing data
    labels = labels.set_index("gsm_name")
    m = get_combined_matrix(names)
    m.to_csv("m.combined.csv")

    samples_m = labels.index.intersection(m.columns)
    m = m[samples_m]
    samples = labels \
        .ix[m.columns] \
        .reset_index()
    samples.to_csv("samples.csv")
    m.to_csv("m.csv")
    edata = com.convert_to_r_matrix(m)
    batch = robjects.StrVector(samples.gse_name + '_' +  samples.gpl_name)
    pheno = robjects.FactorVector(samples.sample_class)
    r.library("sva")
    fmla = robjects.Formula('~pheno')
    # fmla.environment['pheno'] = r['as.factor'](pheno)
    fmla.environment['pheno'] = pheno
    mod = r['model.matrix'](fmla)
    r_combat_edata = r.ComBat(dat=edata, batch=batch, mod=mod)
    combat = pd.DataFrame(np.asmatrix(r_combat_edata))
    combat.index = m.index
    combat.columns = m.columns
    return combat

def get_imputed(data):
    data.to_csv("data.csv")
    r.library("impute")
    r_data = com.convert_to_r_matrix(data)
    r_imputedData = r['impute.knn'](r_data)
    npImputedData = np.asarray(r_imputedData[0])
    imputedData = pd.DataFrame(npImputedData)
    imputedData.index = data.index
    imputedData.columns = data.columns
    return imputedData

def get_annotations(case_query, control_query, modifier_query=""):
    # Fetch all relevant data
    queries = [case_query, control_query, modifier_query]
    tokens = set(cat(re_all('[a-zA-Z]\w*', query) for query in queries))
    df = pd.read_sql_query('''
            SELECT
                sample_id,
                sample.gsm_name,
                annotation,
                series_annotation.series_id,
                series.gse_name,
                series_annotation.platform_id,
                platform.gpl_name,
                tag.tag_name
            FROM
                sample_annotation
                JOIN sample ON (sample_annotation.sample_id = sample.id)
                JOIN series_annotation ON (sample_annotation.serie_annotation_id = series_annotation.id)
                JOIN platform ON (series_annotation.platform_id = platform.id)
                JOIN tag ON (series_annotation.tag_id = tag.id)
                JOIN series ON (series_annotation.series_id = series.id)

            WHERE
                tag.tag_name ~* %(tags)s
        ''', conn, params={'tags': '^(%s)$' % '|'.join(map(re.escape, tokens))})
    # Make tag columns
    df.tag_name = df.tag_name.str.lower()
    df.annotation = df.annotation.str.lower()
    for tag in tokens:
        tag_name = tag.lower()
        df[tag_name] = df[df.tag_name == tag_name].annotation

    # Select only cells with filled annotations
    df = df.drop(['tag_name', 'annotation'], axis=1)
    df = df.groupby(['sample_id', 'series_id', 'platform_id', 'gsm_name', 'gpl_name'],
                    as_index=False).first()

    df = df.convert_objects(convert_numeric=True)

    # Apply case/control/modifier
    if modifier_query:
        df = df.query(modifier_query.lower())
    case_df = df.query(case_query.lower())
    control_df = df.query(control_query.lower())

    # Set 0 and 1 for analysis
    overlap_df = df.ix[set(case_df.index).intersection(set(control_df.index))]

    df['sample_class'] = None
    df['sample_class'].ix[case_df.index] = 1
    df['sample_class'].ix[control_df.index] = 0
    df['sample_class'].ix[overlap_df.index] = -1

    return df.dropna(subset=["sample_class"])

def dropMissingSamples(data, naLimit=0.8):
    """Filters a data frame to weed out cols with missing data"""
    thresh = len(data.index) * (1 - naLimit)
    return data.dropna(thresh=thresh, axis="columns")


def dropMissingGenes(data, naLimit=0.5):
    """Filters a data frame to weed out cols with missing data"""
    thresh = len(data.columns) * (1 - naLimit)
    return data.dropna(thresh=thresh, axis="rows")


def translateNegativeCols(data):
    """Translate the minimum value of each col to 1"""
    data = data.replace([np.inf, -np.inf], np.nan) #replace infinities
    return data + np.abs(np.min(data)) + 1
    # for sample in data.columns:
    # sampleMin = data[sample].min()
    # if sampleMin < 1:
    # absMin = abs(sampleMin)
    # data[sample] = data[sample].add(absMin + 1)
    # return data


def cleanData(data):
    """convenience function to trannslate the data before analysis"""
    if not data.empty:
        # data = getLogged(translateNegativeCols(data))
        data = getLogged(dropMissingSamples(data))
    return data


def isLogged(data):
    return True if (data.std() < 10).all() else False


def getLogged(data):
    # if (data.var() > 10).all():
    if isLogged(data):
        return data
    return translateNegativeCols(np.log2(data))

if __name__ == "__main__":
    print "OK?"
    labels = get_annotations("""DHF=='DHF' or DSS=='DSS'""",
                    """DH=='DH'""",
                             """Dengue_Acute=="Dengue_Acute" or Dengue_Early_Acute=='Dengue_Early_Acute' or Dengue_Late_Acute == 'Dengue_Late_Acute' or Dengue_DOF < 10""")
    names = labels[['gse_name', 'gpl_name']].drop_duplicates().to_records(index=False)

    combat = get_combat(names, labels)
