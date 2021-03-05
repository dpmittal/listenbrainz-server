from flask import current_app
from pyspark.mllib.linalg.distributed import CoordinateMatrix, MatrixEntry
from pyspark.ml.stat import Correlation
from pyspark.sql.functions import struct, collect_list

import listenbrainz_spark
from listenbrainz_spark import SparkSessionNotInitializedException, utils, path, sql_context
from listenbrainz_spark.exceptions import PathNotFoundException, FileNotFetchedException


def create_messages(similar_users_df):
    itr = similar_users_df.toLocalIterator()
    message = {}
    for row in itr:
        message[row.user_name] = {user.second_user_name: user.similarity for user in row.similar_users}
    yield message


def threshold_similar_users(matrix, threshold):
    rows, cols = matrix.shape
    similar_users = list()
    for x in range(rows):
        for y in range(cols):
            if x == y:
                continue
            similarity = float(matrix[x, y])
            if similarity >= threshold:
                similar_users.append((x, y, similarity))
    return similar_users


def main(threshold):
    try:
        listenbrainz_spark.init_spark_session('User Similarity')
    except SparkSessionNotInitializedException as err:
        current_app.logger.error(str(err), exc_info=True)
        raise

    try:
        playcounts_df = utils.read_files_from_HDFS(path.USER_SIMILARITY_PLAYCOUNTS_DATAFRAME)
        users_df = utils.read_files_from_HDFS(path.USER_SIMILARITY_USERS_DATAFRAME)
    except PathNotFoundException as err:
        current_app.logger.error(str(err), exc_info=True)
        raise
    except FileNotFetchedException as err:
        current_app.logger.error(str(err), exc_info=True)
        raise

    tuple_mapped_rdd = playcounts_df.rdd.map(lambda x: MatrixEntry(x["recording_id"], x["user_id"], x["count"]))
    coordinate_matrix = CoordinateMatrix(tuple_mapped_rdd)
    indexed_row_matrix = coordinate_matrix.toIndexedRowMatrix()
    vectors_mapped_rdd = indexed_row_matrix.rows.map(lambda r: (r.index, r.vector.asML()))
    vectors_df = listenbrainz_spark.session.createDataFrame(vectors_mapped_rdd, ['index', 'vector'])

    similarity_matrix = Correlation.corr(vectors_df, 'vector', 'pearson').first()['pearson(vector)'].toArray()
    similar_users = threshold_similar_users(similarity_matrix, threshold)

    other_users_df = users_df\
        .withColumnRenamed('user_id', 'other_user_id')\
        .withColumnRenamed('user_name', 'other_user_name')

    similar_users_df = sql_context.createDataFrame(similar_users, ['user_id', 'other_user_id', 'similarity'])\
        .join(users_df, 'user_id', 'inner')\
        .join(other_users_df, 'other_user_id', 'inner')\
        .select('user_name', struct('other_user_name', 'similarity').alias('similar_user'))\
        .groupBy('user_name')\
        .agg(collect_list('similar_user').alias('similar_users'))

    return create_messages(similar_users_df)
